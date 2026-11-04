"""LLM 제공자. 모델 이름과 키는 전부 .env에서 온다.

- 분석: Gemini generateContent (공개 YouTube URL을 file_data로 직접 입력)
- 작성·심사: Anthropic Messages API (structured outputs)
"""
from __future__ import annotations

import json
from typing import Any

import anthropic
import httpx

from .settings import settings

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


class ProviderError(Exception):
    pass


def _redact(text: str) -> str:
    for k in (settings.youtube_api_key, settings.gemini_api_key, settings.anthropic_api_key):
        if k:
            text = text.replace(k, "***")
    return text


def gemini_json(prompt: str, video_url: str | None = None, model: str | None = None,
                end_offset_sec: int | None = None) -> dict:
    parts: list[dict] = []
    if video_url:
        part: dict = {"file_data": {"file_uri": video_url}}
        if end_offset_sec:  # 긴 영상은 앞부분만 분석해 시간·비용을 제한
            part["video_metadata"] = {"start_offset": "0s", "end_offset": f"{end_offset_sec}s"}
        parts.append(part)
    parts.append({"text": prompt})
    body = {"contents": [{"parts": parts}], "generationConfig": {"responseMimeType": "application/json"}}
    r = httpx.post(
        GEMINI_URL.format(model=model or settings.analyzer_model),
        headers={"x-goog-api-key": settings.gemini_api_key},
        json=body, timeout=180,
    )
    if r.status_code >= 400:
        raise ProviderError(_redact(f"Gemini {r.status_code}: {r.text[:300]}"))
    data = r.json()
    try:
        text = "".join(p.get("text", "") for p in data["candidates"][0]["content"]["parts"])
        return json.loads(text)
    except (KeyError, IndexError, json.JSONDecodeError) as e:
        raise ProviderError(f"Gemini 응답 해석 실패: {str(data)[:300]}") from e


_client: anthropic.Anthropic | None = None


def _claude() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=settings.anthropic_api_key or None, base_url=settings.anthropic_base_url)
    return _client


def claude_json(model: str, system: str, user: str, schema: dict, effort: str = "medium") -> dict:
    kwargs: dict[str, Any] = dict(
        model=model,
        max_tokens=16000,
        system=system,
        messages=[{"role": "user", "content": user}],
        thinking={"type": "adaptive"},
        output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
    )
    # 거절 시 서버 측 대체 모델로 재시도 (Opus 5.5 / Sonnet 5.5에서 권장)
    if settings.claude_fallbacks and model in {"claude-opus-5-5", "claude-sonnet-5-5"}:
        kwargs.update(betas=["server-side-fallback-2026-07-01"], fallbacks="default")
    try:
        resp = _claude().beta.messages.create(**kwargs)
    except anthropic.RateLimitError as e:
        raise ProviderError("Anthropic 요청 한도 초과. 잠시 뒤 다시 시도하세요.") from e
    except anthropic.APIStatusError as e:
        raise ProviderError(f"Anthropic {e.status_code}: {e.message}") from e
    except anthropic.APIConnectionError as e:
        raise ProviderError("Anthropic 연결 실패") from e
    if resp.stop_reason == "refusal":
        cat = getattr(resp.stop_details, "category", None) if resp.stop_details else None
        raise ProviderError(f"모델이 요청을 거절했습니다 (category={cat}).")
    if resp.stop_reason == "max_tokens":
        raise ProviderError("출력이 max_tokens에서 잘렸습니다.")
    text = next((b.text for b in resp.content if b.type == "text"), "")
    return json.loads(text)
