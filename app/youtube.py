"""YouTube Data API v3 + 채널 RSS. 읽기 전용이며 쓰기 엔드포인트는 쓰지 않는다.

할당량 메모(기본 10,000 units/일):
  search.list 100 · channels/videos/playlistItems.list 1 (50개 묶음)
  → 검색은 하루 1회 채널 발굴에만 쓰고, 감시는 RSS(할당량 0)로 한다.
"""
from __future__ import annotations

import random
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import httpx

from .settings import settings

API = "https://www.googleapis.com/youtube/v3"
RSS = "https://www.youtube.com/feeds/videos.xml?channel_id={}"
NS = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015"}


COST = {"search": 100}
_usage: dict[str, int] = {}
_rss = {"fail": 0, "ok": 0}


def quota_used() -> int:
    return _usage.get(datetime.now(timezone.utc).strftime("%Y-%m-%d"), 0)  # 할당량은 태평양 자정 기준이라 근사치


def rss_available() -> bool:
    """연속 실패가 쌓이면 RSS를 건너뛴다(성공이 한 번이라도 있으면 계속 시도)."""
    return _rss["ok"] > 0 or _rss["fail"] < 5


def _get(path: str, **params) -> dict:
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    _usage[day] = _usage.get(day, 0) + COST.get(path, 1)
    # 키를 URL에 넣으면 오류 메시지·로그·화면에 그대로 찍히므로 헤더로 보낸다.
    r = httpx.get(f"{API}/{path}", params=params, headers={"x-goog-api-key": settings.youtube_api_key}, timeout=20)
    if r.status_code >= 400:
        raise httpx.HTTPStatusError(redact(f"YouTube API {r.status_code} ({path}): {r.text[:200]}"),
                                    request=r.request, response=r)
    return r.json()


def redact(text: str) -> str:
    """혹시 섞인 API 키를 가린다."""
    for k in (settings.youtube_api_key, settings.gemini_api_key, settings.anthropic_api_key):
        if k:
            text = text.replace(k, "***")
    return text


def _chunks(xs: list[str], n: int = 50):
    for i in range(0, len(xs), n):
        yield xs[i:i + n]


def parse_video_id(url_or_id: str) -> str | None:
    s = url_or_id.strip()
    if re.fullmatch(r"[\w-]{11}", s):
        return s
    m = re.search(r"(?:v=|youtu\.be/|shorts/|embed/|live/)([\w-]{11})", s)
    return m.group(1) if m else None


# ── 실제 API ──────────────────────────────────────────────

def discover_channel_ids(keywords: list[str], days: int = 30) -> list[str]:
    after = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    ids: list[str] = []
    for kw in keywords:
        data = _get("search", part="snippet", q=kw, type="video", order="viewCount", maxResults=50,
                    publishedAfter=after, regionCode=settings.region, relevanceLanguage="ko")
        ids += [it["snippet"]["channelId"] for it in data.get("items", [])]
    return list(dict.fromkeys(ids))


def channels_info(ids: list[str]) -> list[dict]:
    out = []
    for chunk in _chunks(ids):
        data = _get("channels", part="snippet,statistics,contentDetails", id=",".join(chunk), maxResults=50)
        for it in data.get("items", []):
            st = it.get("statistics", {})
            out.append({
                "id": it["id"], "title": it["snippet"]["title"],
                "subs": int(st.get("subscriberCount", 0) or 0),
                "uploads": it["contentDetails"]["relatedPlaylists"]["uploads"],
            })
    return out


def recent_upload_ids(uploads_playlist: str, n: int = 12) -> list[str]:
    data = _get("playlistItems", part="contentDetails", playlistId=uploads_playlist, maxResults=n)
    return [it["contentDetails"]["videoId"] for it in data.get("items", [])]


_DUR = re.compile(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?")
SHORTS_MAX_SEC = 180  # 2024-10부터 쇼츠는 최대 3분


def parse_duration(iso: str) -> int:
    """ISO 8601 기간(PT1M5S 등)을 초로 바꾼다."""
    m = _DUR.fullmatch(iso or "")
    if not m:
        return 0
    d, h, mi, s = (int(x or 0) for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + s


def shorts_url_check(video_id: str) -> bool | None:
    """/shorts/ID가 200이면 쇼츠, 리디렉션(303)이면 일반 영상. 할당량을 쓰지 않는다."""
    try:
        r = httpx.head(f"https://www.youtube.com/shorts/{video_id}", follow_redirects=False, timeout=10)
    except httpx.HTTPError:
        return None
    if r.status_code == 200:
        return True
    if 300 <= r.status_code < 400:
        return False
    return None


def classify_short(video_id: str, duration_sec: int, title: str = "") -> bool:
    if duration_sec and duration_sec <= 60:
        return True
    if duration_sec > SHORTS_MAX_SEC:
        return False
    checked = shorts_url_check(video_id)  # 61~180초 또는 길이 미상
    if checked is not None:
        return checked
    return "#shorts" in title.lower()


def videos_info(ids: list[str]) -> list[dict]:
    out = []
    for chunk in _chunks(ids):
        data = _get("videos", part="snippet,statistics,contentDetails", id=",".join(chunk), maxResults=50)
        for it in data.get("items", []):
            sn, st = it["snippet"], it.get("statistics", {})
            dur = parse_duration(it.get("contentDetails", {}).get("duration", ""))
            out.append({
                "duration_sec": dur, "is_short": int(classify_short(it["id"], dur, sn["title"])),
                "id": it["id"], "channel_id": sn["channelId"], "channel_title": sn["channelTitle"],
                "title": sn["title"], "description": sn.get("description", "")[:1500],
                "published_at": sn["publishedAt"],
                "thumb": sn.get("thumbnails", {}).get("medium", {}).get("url", ""),
                "views": int(st.get("viewCount", 0) or 0),
            })
    return out


def rss_latest(channel_id: str) -> list[dict]:
    r = httpx.get(RSS.format(channel_id), timeout=15)
    if r.status_code >= 400:
        _rss["fail"] += 1
    r.raise_for_status()
    _rss["ok"] += 1
    root = ET.fromstring(r.text)
    out = []
    for e in root.findall("a:entry", NS):
        out.append({
            "id": e.findtext("yt:videoId", namespaces=NS),
            "title": e.findtext("a:title", namespaces=NS),
            "published_at": e.findtext("a:published", namespaces=NS),
        })
    return out


def latest_upload_ids(channel_id: str, uploads_playlist: str, n: int = 3) -> tuple[list[str], str]:
    """RSS(할당량 0)를 먼저 쓰고, 막혀 있으면 playlistItems(1 unit)로 대체한다."""
    if rss_available():
        try:
            return [e["id"] for e in rss_latest(channel_id)[:n]], "rss"
        except httpx.HTTPError:
            pass
    return recent_upload_ids(uploads_playlist, n), "api"


# ── 목업 (키가 없거나 MOCK_YOUTUBE=true) ──────────────────

_MOCK_CHANNELS = [
    ("mock_love_lab", "연애심리연구소(목업)", 182000, "love"),
    ("mock_tarot_moon", "달빛타로(목업)", 64000, "fortune"),
    ("mock_saju_note", "사주노트(목업)", 238000, "fortune"),
    ("mock_reunion", "재회상담소(목업)", 41000, "love"),
    ("mock_dating_talk", "썸과연애사이(목업)", 97000, "love"),
]
_MOCK_TITLES = [
    "남자가 연락 줄일 때 진짜 속마음 3가지",
    "재회 가능성 높은 이별의 공통점",
    "이번 달 연애운 강하게 들어오는 띠",
    "짝사랑 끝내야 할 때 나타나는 신호",
    "궁합 안 좋다는 말 듣고 헤어져야 할까?",
    "속마음 타로: 그 사람은 지금 나를 생각할까",
]


def mock_channels() -> list[dict]:
    return [{"id": c, "title": t, "subs": s, "uploads": f"UU_{c}", "_cat": cat} for c, t, s, cat in _MOCK_CHANNELS]


def mock_video(channel: dict, hours_ago: float | None = None) -> dict:
    h = hours_ago if hours_ago is not None else random.uniform(0.2, 96)
    vid = "mk" + "".join(random.choice("abcdefghijkmnpqrstuvwxyz23456789") for _ in range(9))
    vph = random.uniform(40, 400)
    dur = random.choice([random.randint(15, 59), random.randint(300, 1500)])
    return {
        "duration_sec": dur, "is_short": int(dur <= 60),
        "id": vid, "channel_id": channel["id"], "channel_title": channel["title"],
        "title": random.choice(_MOCK_TITLES), "description": "목업 영상 설명입니다.",
        "published_at": (datetime.now(timezone.utc) - timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "thumb": "", "views": int(vph * max(h, 0.2)),
    }
