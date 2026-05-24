"""한 번 실행 — 두 계정(normal, work) OAuth 인증 후 tokens.json 갱신.

사용법:
    poc/.venv/bin/python scripts/setup_google_calendar_oauth.py

실행 시:
1. 브라우저가 자동으로 열림 (normal 계정 인증)
2. Google 로그인 + 캘린더 read 권한 허용
3. 동일하게 work 계정도 진행
4. ~/.config/google-calendar-mcp/tokens.json 갱신 완료
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

CREDS_PATH = Path.home() / "Documents/Coding/_tools/claude_code_agent/config/google-credentials.json"
TOKENS_PATH = Path.home() / ".config/google-calendar-mcp/tokens.json"
SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]


def _flow_for_account(account_label: str) -> dict:
    print(f"\n=== {account_label} 계정 인증 시작 ===")
    print("브라우저가 열립니다. Google 로그인 후 캘린더 read 권한을 허용해 주세요.")
    print(f"(만약 두 계정 사용하시면, {account_label}로 로그인하세요.)")
    input("Enter를 누르면 브라우저가 열립니다...")

    flow = InstalledAppFlow.from_client_secrets_file(str(CREDS_PATH), SCOPES)
    creds = flow.run_local_server(port=0, prompt="consent", access_type="offline")

    return {
        "access_token": creds.token,
        "refresh_token": creds.refresh_token,
        "scope": " ".join(creds.scopes),
        "token_type": "Bearer",
        "expiry_date": int(creds.expiry.timestamp() * 1000) if creds.expiry else None,
    }


def main() -> int:
    if not CREDS_PATH.exists():
        print(f"❌ OAuth credentials 파일 없음: {CREDS_PATH}", file=sys.stderr)
        return 1

    print(f"OAuth credentials: {CREDS_PATH}")
    print(f"저장 경로: {TOKENS_PATH}")

    tokens = {}
    if TOKENS_PATH.exists():
        try:
            tokens = json.loads(TOKENS_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass

    # normal
    tokens["normal"] = _flow_for_account("normal (개인)")
    TOKENS_PATH.parent.mkdir(parents=True, exist_ok=True)
    TOKENS_PATH.write_text(json.dumps(tokens, indent=2, ensure_ascii=False))
    print("✅ normal 저장 완료")

    # work
    print("\n다음은 work 계정 차례입니다.")
    print("브라우저에서 현재 로그인된 Google 계정을 로그아웃하거나 다른 브라우저 프로필을 쓰세요.")
    time.sleep(2)
    tokens["work"] = _flow_for_account("work (업무)")
    TOKENS_PATH.write_text(json.dumps(tokens, indent=2, ensure_ascii=False))
    print("✅ work 저장 완료")

    print(f"\n🎉 완료. {TOKENS_PATH}")
    print("이제 morning_briefing이 캘린더 일정을 자동으로 가져옵니다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
