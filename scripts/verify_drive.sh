#!/usr/bin/env bash
# verify_drive.sh — READ-ONLY. Verifies the model-depot manifest shape offline,
# and (only if a live manifest exists locally) re-verifies it against Drive.
# Usage: ./scripts/verify_drive.sh
# Offline step (always): validate config/model_manifest.example.json with
# h3_contract.validate_model_cache_manifest — no network, no credentials.
# Live step (only when model_cache/verified_manifest.json exists on disk):
#   python3 drive_depot.py verify --colab-auth adc
#   python3 drive_depot.py audit-acl --colab-auth adc
# Both live steps read Drive metadata only; they never upload, seed, or
# overwrite anything. audit-acl additionally checks that no depot /
# archive / cache / lease object is shared with "anyone" and fails
# closed (non-zero) on any public exposure. A fresh checkout has no live
# manifest, so only the offline step runs — that is expected, not a failure.
set -euo pipefail
cd "$(dirname "$0")/.."
python3 - <<'PY'
import json
from pathlib import Path
from h3_contract import validate_model_cache_manifest
d = json.loads(Path("config/model_manifest.example.json").read_text())
validate_model_cache_manifest(d)
print(f"OFFLINE manifest example OK: status={d['status']} files={len(d['files'])} bytes={sum(f['size'] for f in d['files'])}")
PY
if [ -f model_cache/verified_manifest.json ]; then
  echo "--- live: drive_depot.py verify (read-only) ---"
  python3 drive_depot.py verify --colab-auth adc
  echo "--- live: drive_depot.py audit-acl (read-only) ---"
  python3 drive_depot.py audit-acl --colab-auth adc
else
  echo "No live model_cache/verified_manifest.json on disk; live Drive re-verification skipped (expected in a fresh checkout)."
fi
