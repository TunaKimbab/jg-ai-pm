"""로컬 대시보드 서버.

실행: .venv/bin/uvicorn app.main:app --port 8787
이 서버는 YouTube에 아무것도 쓰지 않는다. 댓글 입력은 web/index.html 안의 시뮬레이터에서만 일어난다.
"""
from __future__ import annotations

import asyncio
import json
import random
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from . import db, engine, youtube
from .providers import ProviderError
from .settings import settings

WEB = Path(__file__).resolve().parent.parent / "web"
con = db.connect()
_busy: set[str] = set()


# ── 공통 ────────────────────────────────────────────────

def _upsert_video(v: dict, source: str) -> bool:
    """새 영상이면 True."""
    exists = con.execute("SELECT view_history FROM videos WHERE id=?", (v["id"],)).fetchone()
    point = {"t": db.now(), "views": v.get("views", 0)}
    if exists:
        hist = json.loads(exists["view_history"] or "[]")[-20:] + [point]
        con.execute("UPDATE videos SET views=?, view_history=?, duration_sec=COALESCE(?, duration_sec),"
                    " is_short=COALESCE(?, is_short) WHERE id=?",
                    (v.get("views", 0), db.j(hist), v.get("duration_sec"), v.get("is_short"), v["id"]))
        con.commit()
        return False
    con.execute(
        "INSERT INTO videos(id,channel_id,channel_title,title,description,published_at,thumb,views,detected_at,source,"
        "view_history,duration_sec,is_short) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (v["id"], v["channel_id"], v.get("channel_title", ""), v["title"], v.get("description", ""),
         v["published_at"], v.get("thumb", ""), v.get("views", 0), db.now(), source, db.j([point]),
         v.get("duration_sec"), v.get("is_short")),
    )
    con.commit()
    return True


def _video_view(r: dict) -> dict:
    r = dict(r)
    pub = datetime.fromisoformat(r["published_at"].replace("Z", "+00:00"))
    hours = max((datetime.now(timezone.utc) - pub).total_seconds() / 3600, 0.1)
    r["hours"] = round(hours, 1)
    r["vph"] = round(r["views"] / max(hours, 1.0), 1)
    r["rising"] = engine.is_rising(r)
    det = datetime.fromisoformat(r["detected_at"])
    r["detect_lag_min"] = round((det - pub).total_seconds() / 60, 1) if r["source"] == "watcher" else None
    d = con.execute("SELECT thread, error FROM drafts WHERE video_id=?", (r["id"],)).fetchone()
    r["draft"] = ("running" if r["id"] in _busy else "ready" if d and d["thread"]
                  else "sensitive" if d and (d["error"] or "").startswith("SENSITIVE") else "error" if d and d["error"] else None)
    r["done"] = bool(con.execute("SELECT 1 FROM sim_threads WHERE video_id=?", (r["id"],)).fetchone())
    r.pop("view_history", None)
    r.pop("description", None)
    return r


FORMATS = {"auto": "자동 (롱폼+숏폼)", "long": "롱폼만", "short": "숏폼만"}


def current_format() -> str:
    f = db.get_kv(con, "video_format", settings.video_format)
    return f if f in FORMATS else "auto"


def format_ok(v: dict, fmt: str | None = None) -> bool:
    """롱폼 모드는 롱폼만, 숏폼 모드는 숏폼만, 자동은 둘 다. 형식을 모르는 영상은 자동에서만 다룬다."""
    fmt = fmt or current_format()
    if fmt == "auto":
        return True
    if v.get("is_short") is None:
        return False
    return bool(v["is_short"]) == (fmt == "short")


def current_mode() -> str:
    m = db.get_kv(con, "brand_mode", settings.brand_mode)
    return m if m in engine.MODES else "context"


def _make_draft(video_id: str, force: bool = False, refine: bool = False) -> None:
    _busy.add(video_id)
    try:
        v = db.rows(con, "SELECT * FROM videos WHERE id=?", video_id)
        if not v:
            return
        main_p, reply_p = db.assign_personas(con)
        mode = current_mode()
        db.event(con, "draft", f"분석·작성 시작 [{engine.MODES[mode]}]: {v[0]['title'][:40]}", video_id)
        try:
            out = (engine.refine(v[0], main_p, mode=mode, force=force) if refine
                   else engine.build_thread(v[0], main_p, reply_p, mode=mode, force=force))
            if refine:
                hr = out.get("harness_run", {})
                db.event(con, "harness", f"하니스 개선: 선택 {hr.get('chosen')} · 채택 {'예' if hr.get('adopted') else '아니오(기본 유지)'}"
                         + (f" · 교훈: {hr['lesson'][:60]}" if hr.get("lesson") else ""), video_id)
            con.execute(
                "INSERT OR REPLACE INTO drafts(video_id,analysis,thread,predicted,candidates,models,created_at,error)"
                " VALUES(?,?,?,?,?,?,?,NULL)",
                (video_id, db.j(out["analysis"]), db.j(out["thread"]), db.j(out["predicted"]),
                 db.j(out["candidates"]), db.j({"engine": out.get("engine"), **settings.public_summary()}), db.now()),
            )
            con.commit()
            db.event(con, "draft", "초안 완료", video_id)
        except engine.SensitiveTopic as e:
            con.execute("INSERT OR REPLACE INTO drafts(video_id,created_at,error) VALUES(?,?,?)",
                        (video_id, db.now(), "SENSITIVE: " + str(e)))
            con.commit()
            db.event(con, "filtered", f"민감 주제로 제외: {str(e)[:120]}", video_id)
        except (ProviderError, ValueError, KeyError, NotImplementedError) as e:
            con.execute("INSERT OR REPLACE INTO drafts(video_id,created_at,error) VALUES(?,?,?)", (video_id, db.now(), str(e)))
            con.commit()
            db.event(con, "error", f"초안 실패: {str(e)[:160]}", video_id)
    finally:
        _busy.discard(video_id)


# ── 채널 선정 ───────────────────────────────────────────

def refresh_channels() -> int:
    if settings.youtube_is_mock:
        chans = youtube.mock_channels()
        vids_by = {c["id"]: [youtube.mock_video(c) for _ in range(10)] for c in chans}
    else:
        ids = youtube.discover_channel_ids(engine.KEYWORDS)
        chans = youtube.channels_info(ids)
        vids_by = {}
        all_ids: dict[str, str] = {}
        for c in chans:
            for vid in youtube.recent_upload_ids(c["uploads"]):
                all_ids[vid] = c["id"]
        for v in youtube.videos_info(list(all_ids)):
            vids_by.setdefault(v["channel_id"], []).append(v)
    scored = engine.score_channels(chans, vids_by)
    con.execute("UPDATE channels SET watching=0")
    for i, c in enumerate(scored):
        con.execute(
            "INSERT OR REPLACE INTO channels(id,title,subs,uploads,score,metrics,category,watching,updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (c["id"], c["title"], c["subs"], c["uploads"], c["score"], db.j(c.get("metrics", {})),
             c.get("category", ""), int(i < settings.watchlist_size), db.now()),
        )
        con.execute("INSERT OR REPLACE INTO channel_snapshots(channel_id,day,subs) VALUES(?,?,?)",
                    (c["id"], db.today(), c["subs"]))
    con.commit()
    db.event(con, "channels", f"채널 {len(scored)}개 점수 계산, 상위 {min(len(scored), settings.watchlist_size)}개 감시")
    return len(scored)


# ── 업로드 감시 ─────────────────────────────────────────

def poll_once(seed: bool = False) -> list[str]:
    watch = db.rows(con, "SELECT * FROM channels WHERE watching=1")
    new_ids: list[str] = []
    if settings.youtube_is_mock:
        if watch and (seed is False) and random.random() < 0.5:
            c = random.choice(watch)
            v = youtube.mock_video({"id": c["id"], "title": c["title"]}, hours_ago=random.uniform(0.02, 0.1))
            if _upsert_video(v, "watcher"):
                new_ids.append(v["id"])
    else:
        fresh: dict[str, str] = {}
        for c in watch:
            try:
                ids, _src = youtube.latest_upload_ids(c["id"], c["uploads"])
                for vid in ids:
                    if not con.execute("SELECT 1 FROM videos WHERE id=?", (vid,)).fetchone():
                        fresh[vid] = c["id"]
            except Exception as e:  # 채널 하나 실패가 전체 감시를 멈추지 않게
                db.event(con, "error", f"업로드 확인 실패 {c['title']}: {str(e)[:80]}")
        if fresh:
            for v in youtube.videos_info(list(fresh)):
                if _upsert_video(v, "seed" if seed else "watcher") and not seed:
                    new_ids.append(v["id"])
    for vid in new_ids:
        db.event(con, "new", "새 영상 감지", vid)
    return new_ids


def poll_interval() -> int:
    """RSS가 막혀 API로 감시하면 하루 예산(WATCH_QUOTA_UNITS)에 맞춰 주기를 늘린다."""
    if settings.youtube_is_mock or youtube.rss_available():
        return settings.poll_interval_sec
    n = con.execute("SELECT COUNT(*) FROM channels WHERE watching=1").fetchone()[0] or 1
    return max(settings.poll_interval_sec, int(86400 * n / max(settings.watch_quota_units, 1)))


async def watcher_loop():
    loop = asyncio.get_running_loop()
    if not con.execute("SELECT 1 FROM channels").fetchone():
        await loop.run_in_executor(None, refresh_channels)
    await loop.run_in_executor(None, poll_once, True)  # 기존 영상은 '본 것'으로 표시만
    while True:
        await asyncio.sleep(poll_interval())
        try:
            new_ids = await loop.run_in_executor(None, poll_once)
            if settings.auto_draft:
                for vid in new_ids:
                    row = db.rows(con, "SELECT is_short FROM videos WHERE id=?", vid)
                    if row and format_ok(row[0]):
                        loop.run_in_executor(None, _make_draft, vid)
                    else:
                        db.event(con, "skip", f"형식 설정({FORMATS[current_format()]})과 달라 자동 초안 건너뜀", vid)
        except Exception as e:  # noqa: BLE001
            db.event(con, "error", f"감시 루프 오류: {str(e)[:120]}")


@asynccontextmanager
async def lifespan(_: FastAPI):
    task = asyncio.create_task(watcher_loop())
    yield
    task.cancel()


app = FastAPI(title="Comment Agent Dashboard (local)", lifespan=lifespan)


# ── API ────────────────────────────────────────────────

class ModeIn(BaseModel):
    mode: str


class LinkIn(BaseModel):
    url: str


class TypedItem(BaseModel):
    role: str
    persona: str
    text: str


class CommitIn(BaseModel):
    typed: list[TypedItem]


class FailIn(BaseModel):
    persona: str
    reason: str


@app.get("/")
def index():
    return FileResponse(WEB / "index.html")


@app.get("/api/status")
def status():
    return settings.public_summary() | {
        "engine": engine.ENGINE, "mode": current_mode(), "modes": engine.MODES,
        "format": current_format(), "formats": FORMATS, "today": db.threads_today(con), "poll_sec": poll_interval(),
        "watch_source": "mock" if settings.youtube_is_mock else ("rss" if youtube.rss_available() else "api"),
        "quota_used": youtube.quota_used(),
    }


class FormatIn(BaseModel):
    format: str


@app.get("/api/format")
def get_format():
    return {"format": current_format(), "formats": FORMATS}


@app.post("/api/format")
def set_format(body: FormatIn):
    if body.format not in FORMATS:
        raise HTTPException(400, "알 수 없는 형식입니다.")
    db.set_kv(con, "video_format", body.format)
    db.event(con, "format", f"영상 형식 변경: {FORMATS[body.format]}")
    return {"format": body.format}


@app.get("/api/mode")
def get_mode():
    return {"mode": current_mode(), "modes": engine.MODES}


@app.post("/api/mode")
def set_mode(body: ModeIn):
    if body.mode not in engine.MODES:
        raise HTTPException(400, "알 수 없는 방식입니다.")
    db.set_kv(con, "brand_mode", body.mode)
    db.event(con, "mode", f"브랜드 방식 변경: {engine.MODES[body.mode]}")
    return {"mode": body.mode}


@app.get("/api/channels")
def channels():
    out = db.rows(con, "SELECT * FROM channels ORDER BY score DESC")
    for c in out:
        c["metrics"] = json.loads(c["metrics"] or "{}")
    return out


@app.post("/api/channels/refresh")
async def channels_refresh():
    n = await asyncio.get_running_loop().run_in_executor(None, refresh_channels)
    return {"count": n}


@app.post("/api/videos/load")
async def videos_load():
    """① 영상 불러오기: 감시 채널의 최신 영상을 목록으로 가져온다."""
    def work():
        watch = db.rows(con, "SELECT * FROM channels WHERE watching=1")
        if settings.youtube_is_mock:
            vids = [youtube.mock_video({"id": c["id"], "title": c["title"]}) for c in watch for _ in range(2)]
        else:
            ids: list[str] = []
            for c in watch:
                try:
                    ids += youtube.latest_upload_ids(c["id"], c["uploads"])[0]
                except Exception as e:  # noqa: BLE001
                    db.event(con, "error", f"업로드 확인 실패 {c['title']}: {str(e)[:80]}")
            ids += [r["id"] for r in db.rows(con, "SELECT id FROM videos WHERE is_short IS NULL LIMIT 50")]  # 길이 미상 채우기
            vids = youtube.videos_info(list(dict.fromkeys(ids)))
        for v in vids:
            _upsert_video(v, "load")
        return len(vids)
    n = await asyncio.get_running_loop().run_in_executor(None, work)
    return {"count": n}


@app.get("/api/videos")
def videos(filter: str = "all"):
    fmt = current_format()
    out = [_video_view(r) for r in db.rows(con, "SELECT * FROM videos ORDER BY published_at DESC LIMIT 300") if format_ok(r, fmt)][:200]
    if filter == "rising":
        out = [v for v in out if v["rising"]]
    elif filter == "new":
        out = [v for v in out if v["source"] == "watcher"]
    return out


@app.post("/api/videos/link")
async def video_link(body: LinkIn):
    vid = youtube.parse_video_id(body.url)
    if not vid:
        raise HTTPException(400, "유튜브 영상 링크가 아닙니다.")
    if settings.youtube_is_mock:
        raise HTTPException(400, "목업 모드에서는 링크 입력을 쓸 수 없습니다. YOUTUBE_API_KEY를 넣어 주세요.")
    info = await asyncio.get_running_loop().run_in_executor(None, youtube.videos_info, [vid])
    if not info:
        raise HTTPException(404, "영상을 찾을 수 없습니다(비공개·삭제 가능).")
    _upsert_video(info[0], "link")
    return {"id": vid}


@app.get("/api/videos/{video_id}")
def video_detail(video_id: str):
    v = db.rows(con, "SELECT * FROM videos WHERE id=?", video_id)
    if not v:
        raise HTTPException(404, "영상 없음")
    d = db.rows(con, "SELECT * FROM drafts WHERE video_id=?", video_id)
    out = _video_view(v[0]) | {"description": v[0]["description"]}
    if d:
        for k in ("analysis", "thread", "predicted", "candidates"):
            out[k] = json.loads(d[0][k]) if d[0][k] else None
        out["error"] = d[0]["error"]
    return out


@app.post("/api/videos/{video_id}/draft")
async def video_draft(video_id: str, force: bool = False):
    """force=true면 민감 주제 필터를 건너뛴다(사용자가 화면에서 직접 선택한 경우)."""
    row = db.rows(con, "SELECT is_short FROM videos WHERE id=?", video_id)
    if row and not format_ok(row[0]):
        raise HTTPException(409, f"현재 형식 설정({FORMATS[current_format()]})에 맞지 않는 영상입니다.")
    _busy.add(video_id)  # 응답 직후 조회에서도 '작성 중'으로 보이게 먼저 표시
    con.execute("DELETE FROM drafts WHERE video_id=?", (video_id,))
    con.commit()
    asyncio.get_running_loop().run_in_executor(None, _make_draft, video_id, force)
    return {"started": True}


@app.post("/api/videos/{video_id}/refine")
async def video_refine(video_id: str, force: bool = False):
    """Harness² 방식: 이 영상으로 하니스를 개선하고, 그 과정에서 고른 결과물을 초안으로 저장한다."""
    if engine.ENGINE != "core":
        raise HTTPException(501, "하니스 개선은 비공개 core 엔진에서만 동작합니다. 공개본에서는 일반 초안 만들기를 쓰세요.")
    row = db.rows(con, "SELECT is_short FROM videos WHERE id=?", video_id)
    if row and not format_ok(row[0]):
        raise HTTPException(409, f"현재 형식 설정({FORMATS[current_format()]})에 맞지 않는 영상입니다.")
    _busy.add(video_id)
    con.execute("DELETE FROM drafts WHERE video_id=?", (video_id,))
    con.commit()
    asyncio.get_running_loop().run_in_executor(None, _make_draft, video_id, force, True)
    return {"started": True}


@app.get("/api/harness")
def harness(video: str | None = None):
    st = engine.harness_state()
    if video:
        return [r for d in st.values() for r in d["runs"] if r["video"] == video]
    return st


@app.post("/api/simulate/{video_id}/check")
def simulate_check(video_id: str):
    try:
        db.check_rules(con, video_id)
    except db.RuleError as e:
        raise HTTPException(409, str(e))
    return {"ok": True, "remaining": settings.daily_max_threads - db.threads_today(con)}


@app.post("/api/simulate/{video_id}/commit")
def simulate_commit(video_id: str, body: CommitIn):
    try:
        db.commit_thread(con, video_id, [t.model_dump() for t in body.typed])
    except db.RuleError as e:
        raise HTTPException(409, str(e))
    db.event(con, "simulated", "시뮬레이션 입력 완료 (유튜브 미전송)", video_id)
    return {"ok": True}


@app.post("/api/simulate/{video_id}/fail")
def simulate_fail(video_id: str, body: FailIn):
    db.log_failure(con, video_id, body.persona, body.reason)
    return {"ok": True}


@app.get("/api/logs")
def logs():
    return db.rows(con, "SELECT l.*, v.title FROM logs l LEFT JOIN videos v ON v.id=l.video_id ORDER BY l.id DESC LIMIT 200")


@app.get("/api/events")
def events(after: int = 0):
    return db.rows(con, "SELECT * FROM events WHERE id>? ORDER BY id DESC LIMIT 50", after)
