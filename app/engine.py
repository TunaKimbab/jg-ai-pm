"""핵심 로직 로더.

비공개 `core/` 패키지가 있으면 그것을 쓰고, 없으면(공개 저장소를 받은 경우)
`app/stub_core.py`의 단순 버전으로 동작한다. 두 모듈은 같은 인터페이스를 가진다.

    KEYWORDS: list[str]                         채널 발굴 검색어
    score_channels(channels, videos_by_ch) -> list[dict]   점수·지표 부여
    is_rising(video) -> bool                    '뜨기 직전' 판정
    build_thread(video, main_persona, reply_persona, mode, force) -> dict   필터→분석→작성→검증→심사
    MODES: dict[str, str]                       브랜드 방식 (disclosed=A, context=B)
    SensitiveTopic: Exception                   민감 주제로 초안을 만들지 않을 때
    refine(video, persona, mode, force) -> dict  Harness² 방식 하니스 개선 1단계 + 결과 초안
    harness_state() -> dict                     영역별 채택 하니스·교훈·최근 실행
"""
from __future__ import annotations

try:  # pragma: no cover - 환경에 따라 다름
    import core as _impl  # type: ignore
    ENGINE = "core"
except ImportError:  # 공개본
    from . import stub_core as _impl  # type: ignore
    ENGINE = "stub"

KEYWORDS = _impl.KEYWORDS
score_channels = _impl.score_channels
is_rising = _impl.is_rising
build_thread = _impl.build_thread
MODES = _impl.MODES
SensitiveTopic = _impl.SensitiveTopic
refine = _impl.refine
harness_state = _impl.harness_state
