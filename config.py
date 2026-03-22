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
    anthropic_api_key: str
    claude_model: str = "claude-sonnet-4-5-20250929"
    claude_max_tokens: int = 64000
    claude_api_timeout: float = 1800.0

    # Notion
    notion_api_key: str
    notion_parent_page_id: str
    notion_database_id: str = ""  # 개인기록_DB (for page creation)
    notion_data_source_id: str = ""  # 개인기록_DB (for schema/query)

    # Watch directory
    watch_dir: str = str(
        Path.home()
        / "Library/Group Containers/group.com.apple.VoiceMemos.shared/Recordings"
    )

    # File stability
    file_stability_interval: float = 2.0
    file_stability_checks: int = 3
    file_stability_timeout: float = 600.0

    # Retry
    retry_max_attempts: int = 2
    retry_base_delay: float = 5.0
    retry_max_delay: float = 30.0

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
