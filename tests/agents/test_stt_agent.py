"""STTAgent — cache-aware mlx-whisper wrapper."""
import hashlib
from pathlib import Path
from unittest.mock import patch

import pytest

from src.agents.stt_agent import STTAgent


@pytest.fixture
def audio_file(tmp_path) -> Path:
    f = tmp_path / "rec.m4a"
    f.write_bytes(b"\x00" * 4096)
    return f


@pytest.fixture
def cache_dir(tmp_path) -> Path:
    d = tmp_path / "stt_cache"
    d.mkdir()
    return d


def test_cache_hit_skips_transcription(audio_file, cache_dir):
    sha = hashlib.sha256(audio_file.read_bytes()).hexdigest()
    (cache_dir / f"{sha}.txt").write_text("CACHED TRANSCRIPT", encoding="utf-8")
    agent = STTAgent(cache_dir=cache_dir)
    with patch("src.agents.stt_agent.process_audio") as mock_proc:
        result = agent.execute({"audio_path": str(audio_file)})
        mock_proc.assert_not_called()
    assert result.ok is True
    assert result.data["transcript"] == "CACHED TRANSCRIPT"
    assert result.data["cache_hit"] is True


def test_cache_miss_runs_stt_and_writes_cache(audio_file, cache_dir):
    sha = hashlib.sha256(audio_file.read_bytes()).hexdigest()
    agent = STTAgent(cache_dir=cache_dir)
    with patch("src.agents.stt_agent.process_audio", return_value="FRESH TRANSCRIPT") as mock_proc:
        result = agent.execute({"audio_path": str(audio_file)})
        mock_proc.assert_called_once()
    assert result.ok is True
    assert result.data["transcript"] == "FRESH TRANSCRIPT"
    assert result.data["cache_hit"] is False
    assert (cache_dir / f"{sha}.txt").read_text(encoding="utf-8") == "FRESH TRANSCRIPT"
