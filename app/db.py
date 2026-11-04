"""SQLite 저장소와 운영 규칙(영상당 1스레드, 하루 상한)."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone, timedelta
from typing import Any

from .settings import settings

KST = timezone(timedelta(hours=9))

SCHEMA = """
CREATE TABLE IF NOT EXISTS channels (
  id TEXT PRIMARY KEY, title TEXT, subs INTEGER, uploads TEXT,
  score REAL, metrics TEXT, category TEXT, watching INTEGER DEFAULT 0, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS channel_snapshots (
  channel_id TEXT, day TEXT, subs INTEGER, views INTEGER, PRIMARY KEY (channel_id, day)
);
CREATE TABLE IF NOT EXISTS videos (
  id TEXT PRIMARY KEY, channel_id TEXT, channel_title TEXT, title TEXT, description TEXT,
  published_at TEXT, thumb TEXT, views INTEGER, detected_at TEXT, source TEXT,
  view_history TEXT DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS drafts (
  video_id TEXT PRIMARY KEY, analysis TEXT, thread TEXT, predicted TEXT,
  candidates TEXT, models TEXT, created_at TEXT, error TEXT
);
-- 영상당 1스레드: PRIMARY KEY가 코드 수준에서 중복을 막는다.
CREATE TABLE IF NOT EXISTS sim_threads (
  video_id TEXT PRIMARY KEY, day TEXT, main_persona TEXT, reply_persona TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS logs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, video_id TEXT, persona TEXT,
  role TEXT, text TEXT, result TEXT, note TEXT
);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, kind TEXT, message TEXT, video_id TEXT
);
"""


def now() -> str:
    return datetime.now(KST).isoformat(timespec="seconds")


def today() -> str:
    return datetime.now(KST).date().isoformat()


def connect() -> sqlite3.Connection:
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(settings.db_path, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    cols = {r[1] for r in con.execute("PRAGMA table_info(videos)")}
    for col in ("duration_sec", "is_short"):
        if col not in cols:
            con.execute(f"ALTER TABLE videos ADD COLUMN {col} INTEGER")
    con.commit()
    return con


def rows(con: sqlite3.Connection, sql: str, *args: Any) -> list[dict]:
    return [dict(r) for r in con.execute(sql, args).fetchall()]


def event(con: sqlite3.Connection, kind: str, message: str, video_id: str | None = None) -> None:
    con.execute("INSERT INTO events(ts,kind,message,video_id) VALUES(?,?,?,?)", (now(), kind, message, video_id))
    con.commit()


def get_kv(con: sqlite3.Connection, k: str, default: str) -> str:
    r = con.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
    return r[0] if r else default


def set_kv(con: sqlite3.Connection, k: str, v: str) -> None:
    con.execute("INSERT OR REPLACE INTO kv(k,v) VALUES(?,?)", (k, v))
    con.commit()


class RuleError(Exception):
    """운영 규칙 위반. 메시지는 화면에 그대로 표시한다."""


def threads_today(con: sqlite3.Connection) -> int:
    return con.execute("SELECT COUNT(*) FROM sim_threads WHERE day=?", (today(),)).fetchone()[0]


def check_rules(con: sqlite3.Connection, video_id: str) -> None:
    if con.execute("SELECT 1 FROM sim_threads WHERE video_id=?", (video_id,)).fetchone():
        raise RuleError("이 영상에는 이미 댓글 스레드를 입력했습니다. (영상당 1개)")
    if threads_today(con) >= settings.daily_max_threads:
        raise RuleError(f"오늘 입력 상한({settings.daily_max_threads}개)에 도달했습니다.")


def assign_personas(con: sqlite3.Connection) -> tuple[str, str | None]:
    """영상마다 돌아가며 배정한다. 답글 페르소나는 본댓글과 반드시 다르다."""
    p = settings.personas
    n = con.execute("SELECT COUNT(*) FROM sim_threads").fetchone()[0]
    main = p[n % len(p)]
    reply = p[(n + 1) % len(p)] if settings.reply_chain_enabled and len(p) > 1 else None
    return main, reply


def commit_thread(con: sqlite3.Connection, video_id: str, typed: list[dict]) -> None:
    """시뮬레이터 입력이 끝난 뒤 호출. 규칙을 다시 검사하고 기록을 남긴다."""
    check_rules(con, video_id)
    main = next((t for t in typed if t.get("role") == "main"), None)
    reply = next((t for t in typed if t.get("role") == "reply"), None)
    if not main:
        raise RuleError("본댓글 입력 기록이 없습니다.")
    try:
        con.execute(
            "INSERT INTO sim_threads(video_id,day,main_persona,reply_persona,created_at) VALUES(?,?,?,?,?)",
            (video_id, today(), main["persona"], reply["persona"] if reply else None, now()),
        )
    except sqlite3.IntegrityError as e:  # 동시 요청 대비
        raise RuleError("이 영상에는 이미 댓글 스레드를 입력했습니다. (영상당 1개)") from e
    for t in typed:
        con.execute(
            "INSERT INTO logs(ts,video_id,persona,role,text,result,note) VALUES(?,?,?,?,?,?,?)",
            (now(), video_id, t["persona"], t["role"], t["text"], "simulated", "웹 내부 시뮬레이션 · 유튜브 미전송"),
        )
    con.commit()


def log_failure(con: sqlite3.Connection, video_id: str, persona: str, reason: str) -> None:
    con.execute(
        "INSERT INTO logs(ts,video_id,persona,role,text,result,note) VALUES(?,?,?,?,?,?,?)",
        (now(), video_id, persona, "-", "", "failed", reason),
    )
    con.commit()


def j(v: Any) -> str:
    return json.dumps(v, ensure_ascii=False)
