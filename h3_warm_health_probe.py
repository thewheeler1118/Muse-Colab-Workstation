"""Remote warm-session health probe (v0.3.0 M4).

Executed via a SHORT `colab exec -f` call before reusing a warm-pooled GPU
session. Verifies that the resident ComfyUI server on `http://127.0.0.1:8188`
answers `/object_info` with the required MiniMax-H3 node types.

Prints `MUSE_WARM_HEALTH_OK <json>` on success and exits non-zero on failure.
"""

from __future__ import annotations

import json
import sys
import urllib.request

REQUIRED_NODES = (
    "MiniMaxH3ReferenceToVideo",
    "SaveVideo",
    "UNETLoader",
    "CLIPLoader",
)
DEFAULT_SERVER = "http://127.0.0.1:8188"


def probe_warm_comfy(server: str = DEFAULT_SERVER, timeout: float = 10.0) -> dict:
    req = urllib.request.Request(server.rstrip("/") + "/object_info", method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        info = json.loads(resp.read().decode("utf-8"))
    if not isinstance(info, dict):
        raise RuntimeError("ComfyUI /object_info did not return a JSON object")
    missing = [node for node in REQUIRED_NODES if node not in info]
    if missing:
        raise RuntimeError(f"Required ComfyUI nodes missing: {', '.join(missing)}")
    return {"ok": True, "node_count": len(info), "required_nodes": list(REQUIRED_NODES)}


def main(server: str = DEFAULT_SERVER) -> None:
    result = probe_warm_comfy(server=server)
    print("MUSE_WARM_HEALTH_OK " + json.dumps(result, separators=(",", ":")), flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        print(f"MUSE_WARM_HEALTH_FAIL: {exc}", file=sys.stderr, flush=True)
        sys.exit(1)
