"""공개용 단순 구현(개요). 실제 점수식·프롬프트·검증 규칙은 비공개 core/에 있다.

이 파일은 전체 흐름이 끝까지 돌아가는 것을 보여 주기 위한 최소 버전이다.
"""
from __future__ import annotations

from datetime import datetime, timezone
from statistics import median

from . import providers
from .settings import settings

KEYWORDS = ["연애 심리", "연애운"]
MODES = {"disclosed": "A · 광고 표기", "context": "B · 맥락 댓글만"}


class SensitiveTopic(Exception):
    pass


def refine(video: dict, persona: str, mode: str = "context", force: bool = False) -> dict:
    raise NotImplementedError("하니스 개선은 비공개 core 엔진에서만 동작합니다.")


def harness_state() -> dict:
    return {}


def _hours(published_at: str) -> float:
    t = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
    return max((datetime.now(timezone.utc) - t).total_seconds() / 3600, 0.1)


def score_channels(channels: list[dict], videos_by_ch: dict[str, list[dict]]) -> list[dict]:
    for c in channels:
        vph = [v["views"] / _hours(v["published_at"]) for v in videos_by_ch.get(c["id"], [])]
        c["metrics"] = {"vph_median": round(median(vph), 1) if vph else 0}
        c["score"] = c["metrics"]["vph_median"]
        c["category"] = "unknown"
    return sorted(channels, key=lambda c: c["score"], reverse=True)


def is_rising(video: dict) -> bool:
    return 1000 <= video["views"] <= 5000 and video["views"] / _hours(video["published_at"]) >= 150


def build_thread(video: dict, main_persona: str, reply_persona: str | None, mode: str = "context",
                 force: bool = False) -> dict:
    if settings.llm_is_mock:
        analysis = {"concern": video["title"], "scenes": [], "mood": "-"}
        text = f"'{video['title'][:30]}' 보고 생각이 많아지네요. 저도 비슷한 고민 중이라 공감됐어요."
        if mode == "disclosed":
            text = f"{settings.disclosure_tag} {text} {settings.brand_name} 연애사주도 참고해 보세요."
    else:
        analysis = providers.gemini_json(
            "영상이 다루는 연애 고민을 한 줄로 JSON {\"concern\": str} 으로 답하세요.",
            f"https://www.youtube.com/watch?v={video['id']}",
        )
        out = providers.claude_json(
            settings.effective_writer_model,
            "영상을 본 시청자로서 40~120자 한국어 댓글을 씁니다.",
            f"영상 제목: {video['title']}\n고민: {analysis.get('concern')}",
            {"type": "object", "properties": {"text": {"type": "string"}},
             "required": ["text"], "additionalProperties": False},
            effort=settings.writer_effort,
        )
        text = out["text"]
    return {
        "analysis": analysis,
        "thread": {"main": {"persona": main_persona, "text": text}, "reply": None, "mode": mode},
        "predicted": {}, "candidates": [], "engine": "stub", "mode": mode,
    }
