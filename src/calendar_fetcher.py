"""Google Calendar 일정 fetcher — morning briefing 상단 표시용.

기존 google-calendar-mcp의 tokens.json + OAuth credentials 재사용. 두 계정 모두 조회.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

log = logging.getLogger(__name__)

TOKENS_PATH = Path.home() / ".config/google-calendar-mcp/tokens.json"
# Same OAuth client used by Claude Code's google-calendar MCP (settings.json: GOOGLE_OAUTH_CREDENTIALS)
CREDS_PATH = Path.home() / "Documents/Coding/_tools/claude_code_agent/config/google-credentials.json"
SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]
KST = timezone(timedelta(hours=9))


def _load_client_config() -> dict:
    raw = json.loads(CREDS_PATH.read_text(encoding="utf-8"))
    return raw.get("installed") or raw.get("web") or raw


def _make_creds(account_tokens: dict) -> Credentials:
    cfg = _load_client_config()
    creds = Credentials(
        token=account_tokens.get("access_token"),
        refresh_token=account_tokens.get("refresh_token"),
        token_uri=cfg["token_uri"],
        client_id=cfg["client_id"],
        client_secret=cfg["client_secret"],
        scopes=SCOPES,
    )
    if not creds.valid:
        try:
            creds.refresh(Request())
        except Exception as e:
            log.warning("token refresh failed: %s", e)
    return creds


def _fetch_account(account_name: str, account_tokens: dict, time_min: datetime, time_max: datetime) -> list[dict]:
    try:
        creds = _make_creds(account_tokens)
        service = build("calendar", "v3", credentials=creds, cache_discovery=False)
        events = service.events().list(
            calendarId="primary",
            timeMin=time_min.isoformat(),
            timeMax=time_max.isoformat(),
            singleEvents=True,
            orderBy="startTime",
            maxResults=50,
        ).execute()
        items = events.get("items", [])
        out: list[dict] = []
        for ev in items:
            start = ev.get("start", {})
            end = ev.get("end", {})
            out.append({
                "account": account_name,
                "summary": ev.get("summary", "(제목 없음)"),
                "start_dt": start.get("dateTime") or start.get("date"),
                "end_dt": end.get("dateTime") or end.get("date"),
                "all_day": "date" in start,
                "location": ev.get("location", ""),
                "attendees": [a.get("email", "") for a in ev.get("attendees", [])],
                "html_link": ev.get("htmlLink", ""),
                "description": ev.get("description", "")[:200],
            })
        return out
    except Exception as e:
        log.warning("calendar fetch failed for %s: %s", account_name, e)
        return []


def fetch_events(target_date: str) -> list[dict]:
    """target_date(YYYY-MM-DD)의 모든 일정을 두 계정에서 조회. KST 기준 00:00~23:59."""
    if not TOKENS_PATH.exists():
        log.warning("tokens.json missing: %s", TOKENS_PATH)
        return []
    tokens = json.loads(TOKENS_PATH.read_text(encoding="utf-8"))

    y, m, d = map(int, target_date.split("-"))
    time_min = datetime(y, m, d, 0, 0, 0, tzinfo=KST)
    time_max = datetime(y, m, d, 23, 59, 59, tzinfo=KST)

    all_events: list[dict] = []
    for account_name, account_tokens in tokens.items():
        if not isinstance(account_tokens, dict):
            continue
        evs = _fetch_account(account_name, account_tokens, time_min, time_max)
        all_events.extend(evs)

    # 중복 제거 — 같은 (summary, start_dt) 는 한 번만, accounts 는 list로 합침
    deduped: dict[tuple, dict] = {}
    for ev in all_events:
        key = (ev.get("summary", ""), ev.get("start_dt", ""))
        if key in deduped:
            existing = deduped[key]
            if ev["account"] not in existing["accounts"]:
                existing["accounts"].append(ev["account"])
        else:
            ev = dict(ev)
            ev["accounts"] = [ev.pop("account")]
            deduped[key] = ev

    out = list(deduped.values())
    out.sort(key=lambda x: x.get("start_dt", ""))
    return out


def format_event_time(start_dt: str, all_day: bool) -> str:
    """ISO datetime → 'HH:MM' or 'all-day'."""
    if all_day:
        return "종일"
    try:
        # 2026-05-13T09:30:00+09:00 또는 +0900
        dt = datetime.fromisoformat(start_dt.replace("Z", "+00:00"))
        return dt.astimezone(KST).strftime("%H:%M")
    except Exception:
        return start_dt[:16]


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)
    target = sys.argv[1] if len(sys.argv) > 1 else datetime.now(KST).date().isoformat()
    evs = fetch_events(target)
    print(f"{target}: {len(evs)} events")
    for e in evs:
        t = format_event_time(e["start_dt"], e["all_day"])
        acc = "+".join(a[:4] for a in e.get("accounts", []))
        print(f"  [{acc}] {t}  {e['summary'][:60]}")
