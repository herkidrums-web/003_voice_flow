"""Orchestrator — coordinates STT → NER → Dict → Grouping → Analysis → Validation → (Self-Heal loop) → Classification → Notion → Wiki."""
from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from src.agents.state import OrchestratorState, Stage, _ORDER

log = logging.getLogger(__name__)


def _extract_rec_date(filename: str) -> str:
    m = re.match(r'(\d{4})(\d{2})(\d{2})', filename)
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else date.today().isoformat()


def _build_meeting_title(rec_date: str, meeting_type: str, customer: str, topics: list[str]) -> str:
    date_str = rec_date.replace("-", "")
    parts = [date_str, meeting_type or "기타"]
    if customer:
        parts.append(customer)
    if topics:
        parts.append("·".join(t[:15] for t in topics[:3]))
    return "_".join(parts)


# Stage index helpers
_IDX = {s: i for i, s in enumerate(_ORDER)}


def _stage_idx(stage: Stage) -> int:
    return _IDX.get(stage.value, 0)


class Orchestrator:
    def __init__(
        self,
        agents: dict,
        state: OrchestratorState,
        max_self_heal: int = 5,
        todo_output_dir: Path | None = None,
    ):
        self.agents = agents
        self.state = state
        self.max_self_heal = max_self_heal
        self.todo_output_dir = todo_output_dir

    def _record_done(self, file: str, stage: Stage, meta: dict | None = None):
        self.state.record(file, stage, status="done", meta=meta or {})

    def _record_fail(self, file: str, stage: Stage, err: str):
        self.state.record(file, stage, status="failed", meta={"error": err})

    def _already_done(self, file: str, stage: Stage) -> bool:
        """Return True if this file has already completed this stage (or a later one)."""
        current_idx = _stage_idx(self.state.get_stage(file))
        return current_idx >= _stage_idx(stage)

    # ── Result cache (analysis + classification results, keyed by group file set) ──
    # Avoids re-calling Claude CLI when Notion/Wiki fails and the orchestrator retries.

    def _group_key(self, group: list[str]) -> str:
        h = hashlib.md5("|".join(sorted(group)).encode()).hexdigest()[:8]
        return f"{group[0].rsplit('.', 1)[0]}_{h}"

    def _result_cache_path(self, group: list[str]) -> Path:
        cache_dir = self.state.path.parent / ".result_cache"
        cache_dir.mkdir(exist_ok=True)
        return cache_dir / f"{self._group_key(group)}.json"

    def _save_result_cache(
        self, group: list[str], analyzed: dict, cls_data: dict, meeting_title: str, rec_date: str
    ) -> None:
        try:
            self._result_cache_path(group).write_text(
                json.dumps(
                    {"analyzed": analyzed, "classified": cls_data,
                     "meeting_title": meeting_title, "rec_date": rec_date},
                    ensure_ascii=False, indent=2,
                ),
                encoding="utf-8",
            )
        except Exception as exc:
            log.warning("result cache write failed: %s", exc)

    def _load_result_cache(self, group: list[str]) -> dict | None:
        p = self._result_cache_path(group)
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return None

    # ── Per-file phases ──

    def _stt_phase(self, audio: Path) -> dict | None:
        if self._already_done(audio.name, Stage.STT):
            # Resume: re-run STTAgent so it loads transcript from cache (fast, < 0.1s)
            r = self.agents["stt"].execute({"audio_path": str(audio)})
            return r.data if r.ok else {"transcript": "", "cache_hit": True, "sha256": ""}
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
                return r.data  # includes analyses, summary, participants, customer, risks

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

    def _notion_wiki_phase(
        self,
        group: list[str],
        analyzed: dict,
        cls_data: dict,
        meeting_title: str,
        rec_date: str,
        joined: str,
    ) -> bool:
        """Run Notion + Wiki for a group. Records state. Returns True if both succeed."""
        n = self.agents["notion"].execute({
            "title": meeting_title,
            "date": rec_date,
            "transcript": joined,
            "analyses": analyzed["analyses"],
            "summary": analyzed.get("summary", ""),
            "participants": analyzed.get("participants", []),
            "customer": analyzed.get("customer", ""),
            "risks": analyzed.get("risks", []),
            "classification": cls_data,
            "source_filename": group[0],
        })
        if not n.ok:
            for f in group:
                self._record_fail(f, Stage.NOTION, n.error)
            return False
        for f in group:
            self._record_done(f, Stage.NOTION, {"notion_url": n.data["notion_url"]})

        w = self.agents["wiki"].execute({
            "title": meeting_title,
            "notion_url": n.data["notion_url"],
            "section": cls_data.get("meeting_type", "Meetings"),
            "date": rec_date,
        })
        if not w.ok:
            for f in group:
                self._record_fail(f, Stage.WIKI, w.error)
            return False
        for f in group:
            self._record_done(f, Stage.WIKI)
        return True

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
        batch_actions: list[dict] = []
        for group in gr.data["groups"]:
            joined = "\n\n".join(per_file_transcript[n] for n in group)
            source_label = group[0].rsplit(".", 1)[0]

            # --- Cache-hit path: classification already done, skip Claude CLI calls ---
            if all(self._already_done(f, Stage.CLASSIFICATION) for f in group):
                cached = self._load_result_cache(group)
                if cached:
                    log.info("skipping analysis+classification (cached): %s", group[0])
                    analyzed = cached["analyzed"]
                    cls_data = cached["classified"]
                    meeting_title = cached["meeting_title"]
                    rec_date = cached["rec_date"]
                    for topic in analyzed["analyses"]:
                        for action in topic.get("actions", []):
                            if action:
                                batch_actions.append({"source": source_label, "action": action})
                    if not self._notion_wiki_phase(group, analyzed, cls_data, meeting_title, rec_date, joined):
                        all_ok = False
                    continue
                log.warning("classification done but cache missing — re-running analysis: %s", group[0])

            # --- Normal path: Analysis → Classification → cache → Notion → Wiki ---
            analyzed = self._analyze_with_self_heal(group, joined)
            if analyzed is None:
                all_ok = False
                continue

            for topic in analyzed["analyses"]:
                for action in topic.get("actions", []):
                    if action:
                        batch_actions.append({"source": source_label, "action": action})

            cls = self.agents["classification"].execute({
                "title": group[0],
                "summary": analyzed.get("summary", "") or joined[:500],
            })
            if not cls.ok:
                for f in group:
                    self._record_fail(f, Stage.CLASSIFICATION, cls.error)
                all_ok = False
                continue
            for f in group:
                self._record_done(f, Stage.CLASSIFICATION)

            rec_date = _extract_rec_date(group[0])
            meeting_title = _build_meeting_title(
                rec_date=rec_date,
                meeting_type=cls.data.get("meeting_type", "기타"),
                customer=analyzed.get("customer", ""),
                topics=[a["topic"] for a in analyzed["analyses"][:3]],
            )

            # Save before Notion so retry path can skip Claude calls
            self._save_result_cache(group, analyzed, cls.data, meeting_title, rec_date)

            if not self._notion_wiki_phase(group, analyzed, cls.data, meeting_title, rec_date, joined):
                all_ok = False
                continue

        # --- TodoAgent: batch actions → next-day to-do ---
        if batch_actions and "todo" in self.agents:
            tomorrow = (date.today() + timedelta(days=1)).isoformat()
            td = self.agents["todo"].execute({
                "batch_actions": batch_actions,
                "target_date": tomorrow,
            })
            if td.ok and self.todo_output_dir:
                out_path = self.todo_output_dir / f"todos_{date.today().isoformat()}.json"
                out_path.write_text(
                    json.dumps(td.data, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                log.info("todo saved: %s (%d projects)", out_path, len(td.data.get("todos", [])))
            elif not td.ok:
                log.warning("TodoAgent failed: %s", td.error)

        return {"ok": all_ok}
