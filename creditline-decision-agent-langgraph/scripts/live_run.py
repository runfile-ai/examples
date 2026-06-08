"""Drive the LangGraph adapter against LIVE PROD ingest and capture what shipped.

Runs the same credit-line graph as ``capture_verify`` / ``trace_compare`` (real
adapter, scripted model + faithful tools, real ``interrupt()`` HITL), but points the
SDK at the **production** ingest (``https://api.eu-west-2.runfile.ai``) with the
test-tenant key. The full real path runs: redact (prod policy) → AES-256-GCM encrypt
→ ``POST /v1/batches`` → prod ingest → SQS → Event Processor → Aurora.

It taps ``Flusher._post_with_retry`` (as the runbook's ``confirm_capture`` does) to
record every batch the SDK shipped AND prod's response (status + accepted_count), so
we can confirm the server accepted everything. The captured wire batches are written
to ``captured-audit/langgraph-prod-run/sdk_batches.jsonl`` for the server-side
verification (§5 of ``live-run-verification-runbook.md``).

Uses a FRESH ``conversation_id`` each run so the run is first-in-conversation
(genesis ``prev_event_hash = zero sentinel``) — avoiding the documented
conversation-reuse genesis ``chain_break`` false alarm.

Requires the prod test key in ``platform/tools/seed-test-tenant/.seed-secrets.local``.
Run with the SDK venv (editable adapter + langgraph + runfile-ai-schemas matching the
deployed EP)::

    ../../sdks/packages/python/.venv/bin/python -m scripts.live_run
"""
from __future__ import annotations

import asyncio
import json
import re
import sys
import uuid
from collections import Counter
from pathlib import Path

from scripts.capture_verify import (
    AGENT_IDENTITY,
    DEMO_REQUEST_ID,
    _OFFICER_RESOLUTION,
    _build_model,
    _build_tools,
)

PROD_BASE_URL = "https://api.eu-west-2.runfile.ai"
EXAMPLE_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = EXAMPLE_ROOT.parents[1]
SECRETS = REPO_ROOT / "platform" / "tools" / "seed-test-tenant" / ".seed-secrets.local"
OUT_DIR = REPO_ROOT / "captured-audit" / "langgraph-prod-run"


def _read_api_key() -> str:
    if not SECRETS.exists():
        sys.exit(f"prod test key not found at {SECRETS}")
    for line in SECRETS.read_text().splitlines():
        m = re.match(r"^api_key\s+(\S+)", line)
        if m:
            return m.group(1)
    sys.exit("no `api_key` line in the seed secrets file")


def main() -> int:
    api_key = _read_api_key()
    conversation_id = f"conv_lg_{uuid.uuid4()}"  # fresh → first-in-conversation
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    batches_file = OUT_DIR / "sdk_batches.jsonl"

    # 1. Tap the flusher to record exactly what ships + prod's response.
    from runfile_ai.flusher import Flusher

    shipped: list[dict] = []
    _orig_post = Flusher._post_with_retry

    def _tap(self, body, idempotency_key):  # type: ignore[no-untyped-def]
        status, payload = _orig_post(self, body, idempotency_key)
        shipped.append({"body": body, "status": status, "response": payload})
        return status, payload

    Flusher._post_with_retry = _tap  # type: ignore[method-assign]

    # 2. Init the SDK against PROD (real policy fetch + real data-key mint + encrypt).
    import runfile_ai
    from runfile_ai.integrations import langgraph as runfile_langgraph

    runfile_langgraph._registry.reset()
    print(f"[live] shipping to {PROD_BASE_URL}  key={api_key[:12]}…  conv={conversation_id}")
    runfile_ai.init(api_key=api_key, environment="production", base_url=PROD_BASE_URL)

    # 3. Build the graph, instrument, run the full credit-line flow incl. HITL.
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.prebuilt import create_react_agent
    from langgraph.types import Command

    agent = create_react_agent(_build_model(), _build_tools(), checkpointer=InMemorySaver())
    agent = runfile_langgraph.instrument(
        agent, agent_identity=AGENT_IDENTITY, conversation_id=conversation_id
    )
    cfg = {"configurable": {"thread_id": conversation_id}, "recursion_limit": 60}

    async def drive() -> None:
        print("[live] intake → bureau/policy → score → escalate → HITL gate…")
        await agent.ainvoke(
            {"messages": [{"role": "user", "content": f"Process credit-line request {DEMO_REQUEST_ID}."}]},
            config=cfg,
        )
        print("[live] resuming with officer override (modify → 12,000)…")
        await agent.ainvoke(Command(resume=_OFFICER_RESOLUTION), config=cfg)

    asyncio.run(drive())

    # 4. Drain everything through the flusher to prod, then stop.
    runfile_ai.flush()
    runfile_ai.shutdown()

    # 5. Persist the captured wire batches (bodies only) for server-side verify.
    with open(batches_file, "w") as f:
        for s in shipped:
            f.write(json.dumps(s["body"]) + "\n")

    # 6. Report: did prod accept everything we shipped?
    items = [it for s in shipped for it in s["body"]["items"]]
    events = [it["event"] for it in items if it["type"] == "event"]
    events.sort(key=lambda e: (e.get("segment_index", 0), e["local_seq"]))
    kinds = Counter(e["action"]["kind"] for e in events)
    run_creates = [it for it in items if it["type"] == "run_create"]
    run_id = run_creates[0]["run"]["run_id"] if run_creates else "?"

    shipped_total = len(items)
    accepted_total = sum((s["response"] or {}).get("accepted_count", 0) for s in shipped)
    statuses = Counter(s["status"] for s in shipped)
    seg0 = [e["local_seq"] for e in events if e.get("segment_index", 0) == 0]
    seg1 = [e["local_seq"] for e in events if e.get("segment_index", 0) == 1]

    print(f"\n{'='*72}\nSHIPPED TO PROD — {run_id}\n{'='*72}")
    print(f"  batches: {len(shipped)}   http statuses: {dict(statuses)}")
    print(f"  items shipped: {shipped_total}   server accepted_count (sum): {accepted_total}")
    print(f"  event kinds: {dict(kinds)}")
    print(f"  seg0 local_seq: {seg0}")
    print(f"  seg1 local_seq: {seg1}")

    checks = {
        "all batches HTTP 200": set(statuses) == {200},
        "server accepted == shipped": accepted_total == shipped_total,
        "run_create present (genesis)": bool(run_creates),
        "run_end present": any(it["type"] == "run_end" for it in items),
        "run_suspend + run_resume": kinds["run_suspend"] >= 1 and kinds["run_resume"] >= 1,
        "tool_call >= tool_result (interrupt dangles 1)": kinds["tool_call"] >= kinds["tool_result"],
        "seg0 local_seq contiguous": seg0 == list(range(len(seg0))),
        "seg1 local_seq contiguous": seg1 == list(range(len(seg1))),
    }
    print("\n  SDK-side / transport checks:")
    for name, ok in checks.items():
        print(f"    {'OK  ' if ok else 'FAIL'} {name}")

    all_ok = all(checks.values())
    print(f"\n{'PASS ✅' if all_ok else 'FAIL ❌'} — wire-side. "
          f"Now verify server materialisation (§5) for run_id:\n  {run_id}")
    print(f"\ncaptured wire batches → {batches_file}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
