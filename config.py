"""Voice Flow configuration and error hierarchy."""
from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

_PROJECT_ROOT = Path(__file__).parent
_ENV_FILE = _PROJECT_ROOT / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(_ENV_FILE), env_file_encoding="utf-8", extra="ignore"
    )

    # HuggingFace
    hf_token: str

    # Anthropic
    anthropic_api_key: str = ""
    claude_cli_path: str = "/Users/swlee/.local/bin/claude"
    claude_model: str = "claude-sonnet-4-6"
    claude_model_light: str = "claude-sonnet-4-6"
    claude_max_tokens: int = 64000
    claude_api_timeout: float = 3600.0

    # Notion
    notion_api_key: str
    notion_parent_page_id: str
    notion_database_id: str = ""  # 개인기록_DB (for page creation)
    notion_data_source_id: str = ""  # 개인기록_DB (for schema/query)

    # Watch directory — recordings_mirror/ to avoid macOS FDA dependency
    watch_dir: str = str(_PROJECT_ROOT / "recordings_mirror")

    # File stability
    file_stability_interval: float = 2.0
    file_stability_checks: int = 3
    file_stability_timeout: float = 600.0

    # Retry
    retry_max_attempts: int = 2
    retry_base_delay: float = 5.0
    retry_max_delay: float = 30.0

    # File grouping
    grouping_daytime_gap: float = 300.0  # 5분 (초) — 낮 파일 간격 기준
    grouping_evening_start_hour: int = 17  # 저녁 기준 시각
    grouping_debounce: float = 60.0  # 와처 디바운스 대기 (초)

    # Minimum recording duration (seconds) — skip shorter recordings
    min_duration: float = 5.0

    # STT cache — SHA256 기반 파일 캐시로 재실행 시 STT 스킵
    stt_cache_dir: str = str(_PROJECT_ROOT / ".stt_cache")

    # pyannote diarization — torchcodec 문제 시 비활성화 가능
    enable_diarization: bool = False

    # State
    processed_log: str = "processed.log"

    # Logging
    log_level: str = "INFO"


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()  # type: ignore[call-arg]
    return _settings


# --- Error hierarchy ---

class VoiceFlowError(Exception):
    """Base error for voice-flow pipeline."""


class STTError(VoiceFlowError):
    """STT/diarization processing failed."""


class SummaryError(VoiceFlowError):
    """Claude API summarization failed."""


class NotionWriteError(VoiceFlowError):
    """Notion page creation failed."""


class RetryableError(VoiceFlowError):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class NonRetryableError(VoiceFlowError):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code
