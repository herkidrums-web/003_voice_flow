"""DictionaryAgent — apply custom_terms.txt and append new entries."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from src.agents.base import BaseAgent


class DictionaryAgent(BaseAgent):
    name = "dictionary"

    def __init__(self, terms_path: Path):
        self.terms_path = Path(terms_path)

    def _load_terms(self) -> dict[str, str]:
        if not self.terms_path.exists():
            return {}
        out: dict[str, str] = {}
        for line in self.terms_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=>" not in line:
                continue
            misheard, correct = line.split("=>", 1)
            out[misheard.strip()] = correct.strip()
        return out

    def _append_terms(self, new_terms: list[dict]) -> None:
        existing = self._load_terms()
        to_add = []
        for entry in new_terms:
            misheard = entry.get("misheard", "").strip()
            correct = entry.get("correct", "").strip()
            if not misheard or not correct:
                continue
            if existing.get(misheard) == correct:
                continue
            to_add.append(f"{misheard}=>{correct}")
        if not to_add:
            return
        with self.terms_path.open("a", encoding="utf-8") as fp:
            for line in to_add:
                fp.write(line + "\n")

    def _apply(self, transcript: str) -> str:
        for misheard, correct in self._load_terms().items():
            transcript = transcript.replace(misheard, correct)
        return transcript

    def _run(self, payload: dict[str, Any]) -> dict[str, Any]:
        new_terms = payload.get("new_terms", [])
        if new_terms:
            self._append_terms(new_terms)
        corrected = self._apply(payload.get("transcript", ""))
        return {"transcript": corrected, "applied_terms_count": len(self._load_terms())}
