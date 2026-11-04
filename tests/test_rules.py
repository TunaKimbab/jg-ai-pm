"""공개 영역 테스트: 운영 규칙(영상당 1스레드, 하루 상한, 페르소나 순환)."""
import importlib
import os


def fresh(tmp_path, **env):
    os.environ.update({"DB_PATH": str(tmp_path / "t.db"), "MOCK_LLM": "true", "MOCK_YOUTUBE": "true", **env})
    import app.settings, app.db
    importlib.reload(app.settings); importlib.reload(app.db)
    return app.db, app.db.connect()


def typed(p="유튜브1"):
    return [{"role": "main", "persona": p, "text": "본댓글"}, {"role": "reply", "persona": "유튜브2", "text": "답글"}]


def test_one_thread_per_video(tmp_path):
    db, con = fresh(tmp_path)
    db.commit_thread(con, "v1", typed())
    try:
        db.commit_thread(con, "v1", typed())
        assert False, "같은 영상 두 번째 입력이 막히지 않음"
    except db.RuleError as e:
        assert "영상당 1개" in str(e)


def test_daily_cap(tmp_path):
    db, con = fresh(tmp_path, DAILY_MAX_COMMENTS="2")
    db.commit_thread(con, "a", typed()); db.commit_thread(con, "b", typed())
    try:
        db.commit_thread(con, "c", typed())
        assert False, "하루 상한이 지켜지지 않음"
    except db.RuleError as e:
        assert "상한" in str(e)


def test_personas_rotate_and_differ(tmp_path):
    db, con = fresh(tmp_path, DAILY_MAX_COMMENTS="10")
    seen = []
    for i in range(3):
        m, r = db.assign_personas(con)
        assert m != r
        seen.append(m)
        db.commit_thread(con, f"v{i}", [{"role": "main", "persona": m, "text": "x"}])
    assert seen == ["유튜브1", "유튜브2", "유튜브3"]


def test_logs_marked_simulated(tmp_path):
    db, con = fresh(tmp_path)
    db.commit_thread(con, "v9", typed())
    res = {r["result"] for r in db.rows(con, "SELECT result FROM logs")}
    assert res == {"simulated"}


def test_parse_duration_and_classify(monkeypatch):
    from app import youtube
    assert youtube.parse_duration("PT45S") == 45
    assert youtube.parse_duration("PT1M5S") == 65
    assert youtube.parse_duration("PT1H2M3S") == 3723
    assert youtube.parse_duration("P1DT1S") == 86401
    assert youtube.parse_duration("") == 0
    monkeypatch.setattr(youtube, "shorts_url_check", lambda vid: None)
    assert youtube.classify_short("x", 30) is True
    assert youtube.classify_short("x", 900) is False
    assert youtube.classify_short("x", 120, "고민 #Shorts") is True      # 확인 실패 시 제목 단서
    monkeypatch.setattr(youtube, "shorts_url_check", lambda vid: False)
    assert youtube.classify_short("x", 120, "#shorts") is False          # 주소 확인이 우선


def test_format_filter(tmp_path):
    db, con = fresh(tmp_path)
    import importlib, app.main as m
    importlib.reload(m)
    short, long_, unknown = {"is_short": 1}, {"is_short": 0}, {"is_short": None}
    assert m.format_ok(short, "auto") and m.format_ok(long_, "auto") and m.format_ok(unknown, "auto")
    assert m.format_ok(short, "short") and not m.format_ok(long_, "short") and not m.format_ok(unknown, "short")
    assert m.format_ok(long_, "long") and not m.format_ok(short, "long") and not m.format_ok(unknown, "long")


def test_public_stub_hides_harness(tmp_path, monkeypatch):
    """공개본(core 없음)에서는 하니스 개선 API가 501로 이유를 알려 준다."""
    db, con = fresh(tmp_path)
    import importlib, app.engine as eng, app.stub_core as stub, app.main as m
    from fastapi.testclient import TestClient
    monkeypatch.setattr(eng, "ENGINE", "stub")
    importlib.reload(m)
    m.engine.ENGINE = "stub"
    con2 = m.con
    con2.execute("INSERT OR REPLACE INTO videos(id,channel_id,title,published_at,views,detected_at,source,is_short) VALUES('v1','c','t','2026-11-04T00:00:00Z',1,'x','load',1)")
    con2.commit()
    r = TestClient(m.app).post("/api/videos/v1/refine")
    assert r.status_code == 501 and "공개본" in r.json()["detail"]
    assert stub.harness_state() == {}


def test_youtube_key_not_in_url_or_errors(monkeypatch):
    """API 키는 헤더로만 보내고, 오류 메시지에 남지 않아야 한다."""
    import httpx
    from app import youtube
    old = youtube.settings.youtube_api_key
    object.__setattr__(youtube.settings, "youtube_api_key", "AIzaTESTKEY1234567890abcdefghijklmnopq")  # frozen dataclass
    monkeypatch.setattr(youtube, "_restore_key", lambda: object.__setattr__(youtube.settings, "youtube_api_key", old), raising=False)
    seen = {}
    def fake_get(url, params=None, headers=None, timeout=None):
        seen["url"], seen["params"], seen["headers"] = url, params, headers
        req = httpx.Request("GET", url, params=params)
        return httpx.Response(403, request=req, text="forbidden AIzaTESTKEY1234567890abcdefghijklmnopq")
    monkeypatch.setattr(youtube.httpx, "get", fake_get)
    try:
        youtube.videos_info(["abc"])
        assert False
    except httpx.HTTPStatusError as e:
        assert "AIzaTESTKEY" not in str(e)
    assert "key" not in (seen["params"] or {}) and "AIzaTESTKEY" not in seen["url"]
    assert seen["headers"]["x-goog-api-key"].startswith("AIza")
    youtube._restore_key()
