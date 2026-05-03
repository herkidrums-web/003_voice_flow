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

    # V3 multi-agent model assignments
    claude_model_haiku: str = "claude-haiku-4-5-20251001"
    claude_model_sonnet: str = "claude-sonnet-4-6"
    claude_model_opus: str = "claude-opus-4-7"

    # Self-Heal
    self_heal_max_retries: int = 5

    # Grouping (content-similarity based; time-based rules removed)
    grouping_max_chars: int = 30000

    # Wiki integration
    wiki_index_path: str = "/Users/swlee/Documents/Coding/000_second_brain/wiki/index.md"

    # Notion
    notion_api_key: str
    notion_parent_page_id: str
    notion_database_id: str = ""  # 개인기록_DB (for page creation)
    notion_data_source_id: str = ""  # 개인기록_DB (for schema/query)
    notion_briefing_database_id: str = ""  # 일일 브리핑 DB (사용자가 .env에 주입)

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

    # Maximum recording duration (minutes) — skip accidental long recordings
    # Recordings longer than this are skipped with a macOS notification.
    # Set to 0 to disable the guard. Default: 180 minutes (3 hours).
    max_duration_minutes: float = 180.0

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
