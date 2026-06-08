"""Run the real agent live while capturing the SDK's exact outgoing batches.

Taps Flusher._post_with_retry so every batch the SDK ships to /v1/batches is
written to /tmp/sdk_batches.jsonl (the SDK-side ground truth), then runs the
normal agent end-to-end. Reconcile against the prod DB afterwards.
"""
from __future__ import annotations
import asyncio, json, os, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

OUT = Path("/tmp/sdk_batches.jsonl")
OUT.write_text("")  # truncate

from runfile_ai import flusher as F
_orig = F.Flusher._post_with_retry
def _tap(self, body, idempotency_key):
    try:
        with OUT.open("a") as fh:
            fh.write(json.dumps(body) + "\n")
    except Exception:
        pass
    return _orig(self, body, idempotency_key)
F.Flusher._post_with_retry = _tap

from agent.main import run, DEMO_REQUEST_ID
req = sys.argv[1] if len(sys.argv) > 1 else DEMO_REQUEST_ID
asyncio.run(run(req))
print(f"\n[confirm] SDK batches captured to {OUT}", file=sys.stderr)
