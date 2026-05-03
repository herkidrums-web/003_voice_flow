"""STTAgent — SHA256-cache + mlx-whisper transcription wrapper."""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from src.agents.base import BaseAgent
from src.stt import process_audio


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fp:
        for chunk in iter(lambda: fp.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class STTAgent(BaseAgent):
    name = "stt"

    def __init__(self, cache_dir: Path):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _run(self, payload: dict[str, Any]) -> dict[str, Any]:
        audio_path = Path(payload["audio_path"])
        sha = _sha256(audio_path)
        cache_file = self.cache_dir / f"{sha}.txt"

        if cache_file.exists():
            return {
                "transcript": cache_file.read_text(encoding="utf-8"),
                "cache_hit": True,
                "sha256": sha,
            }

        result = process_audio(str(audio_path))
        # process_audio returns TranscriptResult in production; tests may mock a str.
        transcript = result.full_text if hasattr(result, "full_text") else str(result)
        cache_file.write_text(transcript, encoding="utf-8")
        return {"transcript": transcript, "cache_hit": False, "sha256": sha}
