"""환경 변수(.env) 로더. 모델·API·동작값은 모두 여기서만 읽는다."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    return default if v is None or v == "" else v.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except ValueError:
        return default


def _list(name: str, default: str) -> list[str]:
    return [s.strip() for s in os.getenv(name, default).split(",") if s.strip()]


@dataclass(frozen=True)
class Settings:
    # 모델
    analyzer_model: str = os.getenv("ANALYZER_MODEL", "gemini-3.8-flash")
    writer_model: str = os.getenv("WRITER_MODEL", "claude-opus-5-5")
    judge_model: str = os.getenv("JUDGE_MODEL", "claude-sonnet-5-5")
    test_mode: bool = _bool("TEST_MODE", False)
    test_writer_model: str = os.getenv("TEST_WRITER_MODEL", "claude-haiku-5-5")
    writer_effort: str = os.getenv("WRITER_EFFORT", "medium")
    judge_effort: str = os.getenv("JUDGE_EFFORT", "low")
    claude_fallbacks: bool = _bool("CLAUDE_FALLBACKS", True)
    candidates: int = _int("WRITER_CANDIDATES", 3)
    # Harness² 방식 하니스 개선 (편집·합성·경험 반영 모델)
    harness_model: str = os.getenv("HARNESS_MODEL", "claude-opus-5-5")
    harness_effort: str = os.getenv("HARNESS_EFFORT", "medium")
    harness_enabled: bool = _bool("HARNESS_ENABLED", True)  # 일반 초안에도 채택된 하니스 적용

    # 키
    gemini_api_key: str = os.getenv("GEMINI_API_KEY", "")
    anthropic_api_key: str = os.getenv("ANTHROPIC_API_KEY", "")
    # 셸의 ANTHROPIC_BASE_URL(중계 서버 등)이 섞이지 않도록 .env 값만 쓴다. 비우면 공식 API.
    anthropic_base_url: str = os.getenv("APP_ANTHROPIC_BASE_URL", "") or "https://api.anthropic.com"
    youtube_api_key: str = os.getenv("YOUTUBE_API_KEY", "")

    # 목업
    mock_llm: bool = _bool("MOCK_LLM", False)
    mock_youtube: bool = _bool("MOCK_YOUTUBE", False)

    # 감시·선정
    poll_interval_sec: int = _int("POLL_INTERVAL_SEC", 90)
    watchlist_size: int = _int("WATCHLIST_SIZE", 30)
    auto_draft: bool = _bool("AUTO_DRAFT", True)
    video_format: str = os.getenv("VIDEO_FORMAT", "auto")  # auto | long | short
    longform_analyze_max_sec: int = _int("LONGFORM_ANALYZE_MAX_SEC", 1200)  # 롱폼은 앞 20분만 분석
    watch_quota_units: int = _int("WATCH_QUOTA_UNITS", 6000)  # RSS가 막혔을 때 감시에 쓸 하루 units
    region: str = os.getenv("YOUTUBE_REGION", "KR")

    # 댓글 규칙
    daily_max_threads: int = _int("DAILY_MAX_COMMENTS", 10)
    min_chars: int = _int("COMMENT_MIN_CHARS", 40)
    max_chars: int = _int("COMMENT_MAX_CHARS", 120)
    brand_name: str = os.getenv("BRAND_NAME", "")  # A 방식에서 소개할 브랜드 이름
    brand_mode: str = os.getenv("BRAND_MODE", "context")  # disclosed(A·광고 표기) | context(B·맥락만)
    disclosure_tag: str = os.getenv("DISCLOSURE_TAG", "[광고]")
    sensitive_filter: bool = _bool("SENSITIVE_FILTER", True)
    reply_chain_enabled: bool = False  # 영상당 계정 하나
    personas: list[str] = field(default_factory=lambda: _list("PERSONAS", "유튜브1,유튜브2,유튜브3"))

    # 시뮬레이터
    typing_delay_ms_min: int = _int("TYPING_DELAY_MS_MIN", 60)
    typing_delay_ms_max: int = _int("TYPING_DELAY_MS_MAX", 180)

    db_path: Path = ROOT / os.getenv("DB_PATH", "data/agent.db")

    @property
    def effective_writer_model(self) -> str:
        return self.test_writer_model if self.test_mode else self.writer_model

    @property
    def youtube_is_mock(self) -> bool:
        return self.mock_youtube or not self.youtube_api_key

    @property
    def llm_is_mock(self) -> bool:
        return self.mock_llm or not (self.gemini_api_key and self.anthropic_api_key)

    def public_summary(self) -> dict:
        return {
            "analyzer_model": self.analyzer_model,
            "writer_model": self.effective_writer_model,
            "judge_model": self.judge_model,
            "test_mode": self.test_mode,
            "mock_llm": self.llm_is_mock,
            "mock_youtube": self.youtube_is_mock,
            "daily_max": self.daily_max_threads,
            "personas": self.personas,
            "sensitive_filter": self.sensitive_filter,
            "disclosure_tag": self.disclosure_tag,
            "typing": [self.typing_delay_ms_min, self.typing_delay_ms_max],
            "missing_keys": [k for k, v in {
                "GEMINI_API_KEY": self.gemini_api_key,
                "ANTHROPIC_API_KEY": self.anthropic_api_key,
                "YOUTUBE_API_KEY": self.youtube_api_key,
            }.items() if not v],
        }


settings = Settings()
