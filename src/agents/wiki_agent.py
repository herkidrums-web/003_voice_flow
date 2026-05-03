"""WikiAgent — appends Notion meeting links to second-brain wiki index."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from src.agents.base import BaseAgent


class WikiAgent(BaseAgent):
    name = "wiki"

    def __init__(self, index_path: Path):
        self.index_path = Path(index_path)

    def _run(self, payload: dict[str, Any]) -> dict[str, Any]:
        title = payload["title"]
        url = payload["notion_url"]
        section = payload.get("section", "Meetings")
        date = payload["date"]

        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.index_path.exists():
            self.index_path.write_text("# Wiki Index\n", encoding="utf-8")

        content = self.index_path.read_text(encoding="utf-8")
        if url in content:
            return {"appended": False, "reason": "duplicate"}

        section_header = f"## {section}"
        link_line = f"- {date} — [{title}]({url})"

        if section_header in content:
            lines = content.splitlines()
            for i, line in enumerate(lines):
                if line.strip() == section_header:
                    lines.insert(i + 1, link_line)
                    break
            new_content = "\n".join(lines) + "\n"
        else:
            new_content = content.rstrip() + f"\n\n{section_header}\n{link_line}\n"

        self.index_path.write_text(new_content, encoding="utf-8")
        return {"appended": True, "section": section}
