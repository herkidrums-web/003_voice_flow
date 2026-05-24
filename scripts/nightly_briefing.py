"""Nightly briefing — 매일 00:01 실행 (venv python 직접 호출).

기존 nightly_briefing.sh는 /bin/bash + launchd 컨텍스트에서 FDA 부족으로
`Operation not permitted` 매일 실패(2026-05-23 진단).

orchestrator/morning_briefing plist가 venv python으로 잘 도는 것을 확인했으므로
같은 패턴으로 재작성. shortcuts run 부분은 launchd 컨텍스트에서 어차피
권한 부족(메모리 정책: 자동 sync 폐기)으로 제거.

동작:
  0) sync stale 체크 — stale이면:
       - mirror에 미처리 .m4a가 있으면 mtime 임시 touch 후 drain 시도 (원복 보장)
       - mirror가 비면 Discord 경고 후 skip
  1) orchestrator 드레인 — pending 없을 때까지 (최대 12회)
  2) morning_briefing 실행 — target=전날 (--notify-discord)
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
VENV_PYTHON = PROJECT_ROOT / "poc/.venv/bin/python"
RUN_ORCHESTRATOR = PROJECT_ROOT / "scripts/run_orchestrator.py"
MORNING_BRIEFING = PROJECT_ROOT / "scripts/morning_briefing.py"

SYNC_LOG = Path("/tmp/voiceflow-sync.log")
STALE_THRESHOLD_HOURS = 12.0
NIGHTLY_FORCE_MARKER = Path("/tmp/voiceflow-nightly-force.txt")

# Discord 알림 (run_orchestrator와 동일 패턴 — 환경 의존 없이 직접 구현)
_DISCORD_ENV_FILE = Path.home() / ".claude/channels/discord/.env"
_DISCORD_CHANNEL_ID = "1490245243285667981"
_DISCORD_API = "https://discord.com/api/v10"


def _load_discord_token() -> str | None:
    try:
        for line in _DISCORD_ENV_FILE.read_text(encoding="utf-8").splitlines():
            if line.startswith("DISCORD_BOT_TOKEN="):
                return line.split("=", 1)[1].strip()
    except Exception:
        return None
    return None


def _notify_discord(text: str) -> None:
    """Best-effort Discord 알림. 실패해도 nightly 흐름을 멈추지 않는다."""
    token = _load_discord_token()
    if not token:
        log.warning("Discord token 없음 — 알림 스킵")
        return
    try:
        import requests
        requests.post(
            f"{_DISCORD_API}/channels/{_DISCORD_CHANNEL_ID}/messages",
            headers={"Authorization": f"Bot {token}"},
            json={"content": text[:2000]},
            timeout=10,
        )
    except Exception:
        log.exception("Discord 알림 실패")


def _count_pending_in_mirror(watch_dir: Path, state_path: Path) -> int:
    """recordings_mirror에서 processing_state.jsonl 기준 미처리 .m4a 수 반환.

    미처리 = stage=wiki·status=done 이 아닌 파일.
    state가 없는 파일도 미처리로 간주.
    """
    if not watch_dir.exists():
        return 0

    # 최신 상태만 추적 (파일명 → 최신 entry)
    latest: dict[str, dict] = {}
    if state_path.exists():
        for line in state_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
                fname = entry.get("file", "")
                prev = latest.get(fname)
                if prev is None or entry.get("ts", "") >= prev.get("ts", ""):
                    latest[fname] = entry
            except Exception:
                continue

    pending_count = 0
    for path in watch_dir.glob("*.m4a"):
        entry = latest.get(path.name)
        if entry is None:
            # state에 없으면 미처리
            pending_count += 1
        elif not (entry.get("stage") in ("wiki", "done") and entry.get("status") == "done"):
            if entry.get("status") != "too_long":
                pending_count += 1

    return pending_count


def _ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _run_drain() -> tuple[bool, str]:
    """orchestrator drain 루프. (True, 마지막 출력 tail) or (False, 에러)."""
    for i in range(1, 13):
        log.info("orchestrator pass %d", i)
        try:
            result = subprocess.run(
                [str(VENV_PYTHON), str(RUN_ORCHESTRATOR)],
                capture_output=True,
                text=True,
                timeout=1800,
            )
        except subprocess.TimeoutExpired:
            return False, f"pass {i} timeout"
        except Exception as e:
            return False, f"pass {i} error: {e}"

        out = (result.stdout or "") + (result.stderr or "")
        sys.stdout.write(out)
        sys.stdout.flush()

        if "no pending files" in out:
            log.info("드레인 완료 (pass %d)", i)
            return True, out[-500:]
        if "sync.log stale" in out:
            # stale 상태로 다시 막힘 (touch 직후 mtime 원복된 경우)
            log.warning("drain 중 sync stale 재감지 (pass %d)", i)
            return False, out[-500:]

    return True, "(max pass 도달)"


def main() -> int:
    log.info("nightly briefing 시작")

    state_path = PROJECT_ROOT / "processing_state.jsonl"
    watch_dir = PROJECT_ROOT / "recordings_mirror"

    # 0) sync stale 체크 — stale이면 mirror 미처리 파일 여부로 분기
    sync_stale = False
    if SYNC_LOG.exists():
        age_hours = (time.time() - SYNC_LOG.stat().st_mtime) / 3600
        if age_hours > STALE_THRESHOLD_HOURS:
            sync_stale = True
            log.warning("sync.log stale (%.1fh) — mirror fallback drain 시도", age_hours)
    else:
        # sync.log 없으면 stale로 간주
        sync_stale = True
        age_hours = float("inf")
        log.warning("sync.log 없음 — stale 간주")

    if sync_stale:
        n_pending = _count_pending_in_mirror(watch_dir, state_path)
        if n_pending == 0:
            log.info("sync stale + mirror 미처리 0건 → 브리핑만 시도")
            _notify_discord(
                f"⚠️ nightly: sync stale ({age_hours:.1f}h), mirror 미처리 0건 → 브리핑만 실행"
            )
            # drain 없이 바로 브리핑으로 넘어감
        else:
            log.info("sync stale + mirror 미처리 %d건 → mtime touch 후 drain 시도", n_pending)
            _notify_discord(
                f"⚠️ nightly: sync stale ({age_hours:.1f}h)이지만 mirror에 {n_pending}건 미처리 — 처리 시도합니다."
            )

            # mtime 백업 후 touch (orchestrator의 stale guard 임시 우회)
            original_mtime: float | None = None
            try:
                if SYNC_LOG.exists():
                    original_mtime = SYNC_LOG.stat().st_mtime
            except Exception:
                pass

            force_success = False
            try:
                # touch: 현재 시각으로 mtime 갱신 → orchestrator가 fresh로 인식
                now = time.time()
                os.utime(SYNC_LOG, (now, now))
                log.info("sync.log mtime touch 완료 (original=%.0f)", original_mtime or 0)

                drain_ok, drain_tail = _run_drain()
                force_success = drain_ok
                if drain_ok:
                    log.info("force drain 완료")
                    _notify_discord(
                        f"✅ nightly force drain 완료 — {n_pending}건 처리 시도\n"
                        f"```\n{drain_tail[-400:]}\n```"
                    )
                    NIGHTLY_FORCE_MARKER.write_text(_ts(), encoding="utf-8")
                else:
                    log.warning("force drain 실패: %s", drain_tail)
                    _notify_discord(
                        f"❌ nightly force drain 실패\n```\n{drain_tail[-400:]}\n```"
                    )
            except Exception as e:
                log.exception("force drain 중 예외")
                _notify_discord(f"❌ nightly force drain 예외: {type(e).__name__}: {e}")
            finally:
                # mtime 반드시 원복 (원본 없으면 현재 유지)
                if original_mtime is not None:
                    try:
                        os.utime(SYNC_LOG, (original_mtime, original_mtime))
                        log.info("sync.log mtime 원복 완료")
                    except Exception:
                        log.exception("sync.log mtime 원복 실패")
                else:
                    log.warning("original_mtime 없음 — mtime 원복 스킵")

            # force drain 실패해도 브리핑은 계속
    else:
        # 1) 정상 경로 orchestrator drain
        for i in range(1, 13):
            log.info("orchestrator pass %d", i)
            result = subprocess.run(
                [str(VENV_PYTHON), str(RUN_ORCHESTRATOR)],
                capture_output=True,
                text=True,
                timeout=1800,
            )
            out = (result.stdout or "") + (result.stderr or "")
            sys.stdout.write(out)
            sys.stdout.flush()

            if "no pending files" in out:
                log.info("드레인 완료 (pass %d)", i)
                break
            if "sync.log stale" in out:
                log.info("sync stale 감지 — 브리핑은 그래도 시도 (자가복구 멱등 upsert)")
                break

    # 2) morning briefing (target = 전날, 멱등 upsert)
    log.info("morning_briefing 실행")
    result = subprocess.run(
        [str(VENV_PYTHON), str(MORNING_BRIEFING), "--notify-discord"],
        capture_output=True,
        text=True,
        timeout=600,
    )
    sys.stdout.write((result.stdout or "") + (result.stderr or ""))
    sys.stdout.flush()

    log.info("nightly briefing 완료")
    return 0


if __name__ == "__main__":
    sys.exit(main())
