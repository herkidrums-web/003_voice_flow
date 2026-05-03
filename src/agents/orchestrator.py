"""Orchestrator — coordinates STT → NER → Dict → Grouping → Analysis → Validation → (Self-Heal loop) → Classification → Notion → Wiki."""
from __future__ import annotations

import logging
from datetime import date
from pathlib import Path
from typing import Any

from src.agents.state import OrchestratorState, Stage, _ORDER

log = logging.getLogger(__name__)

# Stage index helpers
_IDX = {s: i for i, s in enumerate(_ORDER)}


def _stage_idx(stage: Stage) -> int:
    return _IDX.get(stage.value, 0)


class Orchestrator:
    def __init__(self, agents: dict, state: OrchestratorState, max_self_heal: int = 5):
        self.agents = agents
        self.state = state
        self.max_self_heal = max_self_heal

    def _record_done(self, file: str, stage: Stage, meta: dict | None = None):
        self.state.record(file, stage, status="done", meta=meta or {})

    def _record_fail(self, file: str, stage: Stage, err: str):
        self.state.record(file, stage, status="failed", meta={"error": err})

    def _already_done(self, file: str, stage: Stage) -> bool:
        """Return True if this file has already completed this stage (or a later one)."""
        current_idx = _stage_idx(self.state.get_stage(file))
        return current_idx >= _stage_idx(stage)

    def _stt_phase(self, audio: Path) -> dict | None:
        if self._already_done(audio.name, Stage.STT):
            # Resume: return placeholder so subsequent phases can proceed
            return {"transcript": "", "cache_hit": True, "sha256": ""}
        r = self.agents["stt"].execute({"audio_path": str(audio)})
        if not r.ok:
            self._record_fail(audio.name, Stage.STT, r.error)
            return None
        self._record_done(audio.name, Stage.STT, {
            "sha256": r.data.get("sha256"),
            "cache_hit": r.data.get("cache_hit"),
        })
        return r.data

    def _ner_phase(self, audio: Path, transcript: str) -> dict | None:
        if self._already_done(audio.name, Stage.NER):
            return {"new_terms": []}
        r = self.agents["ner"].execute({"transcript": transcript, "known_terms": []})
        if not r.ok:
            self._record_fail(audio.name, Stage.NER, r.error)
            return None
        self._record_done(audio.name, Stage.NER, {
            "new_terms_count": len(r.data.get("new_terms", [])),
        })
        return r.data

    def _dict_phase(self, audio: Path, transcript: str, new_terms: list) -> dict | None:
        if self._already_done(audio.name, Stage.DICTIONARY):
            return {"transcript": transcript}
        r = self.agents["dictionary"].execute({
            "transcript": transcript,
            "new_terms": new_terms,
        })
        if not r.ok:
            self._record_fail(audio.name, Stage.DICTIONARY, r.error)
            return None
        self._record_done(audio.name, Stage.DICTIONARY)
        return r.data

    def _analyze_with_self_heal(self, group_files: list[str], transcript: str) -> dict | None:
        params: dict[str, Any] = {"temperature": 0.7, "extra_instruction": ""}
        attempt = 0

        while True:
            r = self.agents["analysis"].execute({"transcript": transcript, **params})
            if not r.ok:
                for f in group_files:
                    self._record_fail(f, Stage.ANALYSIS, r.error)
                return None
            analyses = r.data["analyses"]

            v = self.agents["validation"].execute({
                "analyses": analyses,
                "transcript": transcript,
            })
            if not v.ok:
                for f in group_files:
                    self._record_fail(f, Stage.VALIDATION, v.error)
                return None

            if v.data["pass"]:
                for f in group_files:
                    self._record_done(f, Stage.ANALYSIS)
                    self._record_done(f, Stage.VALIDATION)
                return {"analyses": analyses}

            # Validation failed — ask self_heal whether to retry
            heal = self.agents["self_heal"].execute({
                "issues": v.data["issues"],
                "attempt": attempt,
                "previous_params": params,
            })
            if not heal.ok or not heal.data.get("should_retry"):
                for f in group_files:
                    self._record_fail(f, Stage.VALIDATION, f"self_heal stopped at attempt={attempt}")
                return None

            attempt = heal.data.get("attempt", attempt + 1)
            params = heal.data["next_params"]

            if attempt >= self.max_self_heal:
                for f in group_files:
                    self._record_fail(f, Stage.VALIDATION, "max self-heal retries exceeded")
                return None

    def process_batch(self, audio_files: list[Path]) -> dict:
        per_file_transcript: dict[str, str] = {}

        # --- Per-file phases: STT → NER → Dictionary ---
        for audio in audio_files:
            stt = self._stt_phase(audio)
            if stt is None:
                continue
            transcript = stt.get("transcript", "")

            ner = self._ner_phase(audio, transcript)
            if ner is None:
                continue

            dct = self._dict_phase(audio, transcript, ner.get("new_terms", []))
            if dct is None:
                continue

            per_file_transcript[audio.name] = dct.get("transcript", transcript)

        if not per_file_transcript:
            return {"ok": False, "reason": "no files passed STT/NER/Dict"}

        # --- Grouping (batch) ---
        files_payload = [
            {"name": n, "transcript": t} for n, t in per_file_transcript.items()
        ]
        gr = self.agents["grouping"].execute({"files": files_payload})
        if not gr.ok:
            for n in per_file_transcript:
                self._record_fail(n, Stage.GROUPING, gr.error)
            return {"ok": False, "reason": gr.error}
        for n in per_file_transcript:
            self._record_done(n, Stage.GROUPING)

        # --- Per-group: Analysis+SelfHeal → Classification → Notion → Wiki ---
        all_ok = True
        for group in gr.data["groups"]:
            joined = "\n\n".join(per_file_transcript[n] for n in group)

            analyzed = self._analyze_with_self_heal(group, joined)
            if analyzed is None:
                all_ok = False
                continue

            cls = self.agents["classification"].execute({
                "title": group[0],
                "summary": joined[:1000],
            })
            if not cls.ok:
                for f in group:
                    self._record_fail(f, Stage.CLASSIFICATION, cls.error)
                all_ok = False
                continue
            for f in group:
                self._record_done(f, Stage.CLASSIFICATION)

            today = date.today().isoformat()
            n = self.agents["notion"].execute({
                "title": group[0].rsplit(".", 1)[0],
                "date": today,
                "transcript": joined,
                "analyses": analyzed["analyses"],
                "classification": cls.data,
            })
            if not n.ok:
                for f in group:
                    self._record_fail(f, Stage.NOTION, n.error)
                all_ok = False
                continue
            for f in group:
                self._record_done(f, Stage.NOTION, {"notion_url": n.data["notion_url"]})

            w = self.agents["wiki"].execute({
                "title": group[0].rsplit(".", 1)[0],
                "notion_url": n.data["notion_url"],
                "section": cls.data.get("meeting_type", "Meetings"),
                "date": today,
            })
            if not w.ok:
                for f in group:
                    self._record_fail(f, Stage.WIKI, w.error)
                all_ok = False
                continue
            for f in group:
                self._record_done(f, Stage.WIKI)

        return {"ok": all_ok}
