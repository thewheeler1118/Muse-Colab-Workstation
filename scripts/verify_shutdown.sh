#!/usr/bin/env bash
# verify_shutdown.sh — READ-ONLY by default. Confirms no GPU session is still active.
# Usage: ./scripts/verify_shutdown.sh [--stop-warm] [SESSION_ID]
# Default (no --stop-warm): runs `python3 h3_controller.py check`
# (read-only `colab usage`/`sessions`) and fails unless the sessions
# list is empty and the CU rate is 0.0. If a session id is given, it
# is additionally checked against raw `colab --auth=adc sessions` output
# (the watchdog's session_still_active check).
# With --stop-warm: explicitly reclaims any warm-idle pooled leases via
# `h3_shutdown_executor.py stop --confirm-stop --stop-warm`, then runs the
# standard read-only `check` asserting `sessions == []` and `rate == 0.0`.
set -euo pipefail
cd "$(dirname "$0")/.."

STOP_WARM=0
SESSION_ID=""
while [ $# -gt 0 ]; do
  case "$1" in
    --stop-warm)
      STOP_WARM=1
      shift
      ;;
    -*)
      echo "REFUSED: unknown argument: $1" >&2
      echo "Usage: $0 [--stop-warm] [SESSION_ID]" >&2
      exit 2
      ;;
    *)
      if [ -n "$SESSION_ID" ]; then
        echo "REFUSED: unexpected extra argument: $1" >&2
        echo "Usage: $0 [--stop-warm] [SESSION_ID]" >&2
        exit 2
      fi
      SESSION_ID="$1"
      shift
      ;;
  esac
done

if [ "$STOP_WARM" -eq 1 ]; then
  python3 h3_shutdown_executor.py stop --confirm-stop --stop-warm
fi

OUT="$(python3 h3_controller.py check)"
echo "$OUT"
if ! python3 -c "import json,sys; d=json.loads(sys.argv[1]); r=d.get('rate'); ok=(d.get('sessions')==[] and r is not None and not isinstance(r,bool) and float(r)==0.0); sys.exit(0 if ok else 1)" "$OUT"; then
  echo "SHUTDOWN NOT VERIFIED: active sessions or non-zero CU rate remain (see above)." >&2
  exit 1
fi
if [ -n "$SESSION_ID" ] && command -v colab >/dev/null 2>&1; then
  if colab --auth=adc sessions 2>&1 | grep -q "$SESSION_ID"; then
    echo "SHUTDOWN NOT VERIFIED: session $SESSION_ID still appears in raw sessions output." >&2
    exit 1
  fi
fi
echo "GPU_STOP_VERIFIED: no active sessions."
