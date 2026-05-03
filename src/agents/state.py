"""Orchestrator state machine — JSONL persisted, append-only."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any


class Stage(str, Enum):
    NONE = "none"
    STT = "stt"
    NER = "ner"
    DICTIONARY = "dictionary"
    GROUPING = "grouping"
    ANALYSIS = "analysis"
    VALIDATION = "validation"
    CLASSIFICATION = "classification"
    NOTION = "notion"
    WIKI = "wiki"
    DONE = "done"


_ORDER = [s.value for s in Stage]


@dataclass
class OrchestratorState:
    path: Path

    def record(self, file: str, stage: Stage, status: str, meta: dict[str, Any] | None = None) -> None:
        record = {
            "file": file,
            "stage": stage.value,
            "status": status,
            "meta": meta or {},
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fp:
            fp.write(json.dumps(record, ensure_ascii=False) + "\n")

    def _records_for(self, file: str) -> list[dict]:
        if not self.path.exists():
            return []
        out: list[dict] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
                if rec.get("file") == file:
                    out.append(rec)
            except json.JSONDecodeError:
                continue
        return out

    def get_stage(self, file: str) -> Stage:
        latest = Stage.NONE
        latest_idx = 0
        for rec in self._records_for(file):
            if rec.get("status") != "done":
                continue
            stage = rec.get("stage", "none")
            try:
                idx = _ORDER.index(stage)
            except ValueError:
                continue
            if idx > latest_idx:
                latest_idx = idx
                latest = Stage(stage)
        return latest

    def list_files_at_stage(self, stage: Stage) -> list[str]:
        if not self.path.exists():
            return []
        files: set[str] = set()
        for line in self.path.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("file"):
                files.add(rec["file"])
        return [f for f in files if self.get_stage(f) == stage]
