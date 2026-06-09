"""Drive the OpenAI Agents adapter against LIVE PROD ingest and capture what shipped.

Runs the real ``creditline-decision-agent-openai`` agent (real OpenAI model + the real
``mimic-creditline`` MCP tools, blocking HITL approval gate), but points the SDK at the
**production** ingest (``https://api.eu-west-2.runfile.ai``) with the test-tenant key.
The full real path runs: redact (prod policy) -> AES-256-GCM encrypt -> ``POST
/v1/batches`` -> prod ingest -> SQS -> Event Processor -> Aurora.

Taps ``Flusher._post_with_retry`` (as the runbook's ``confirm_capture`` does) to record
every batch the SDK shipped AND prod's response (status + accepted_count), so we can
confirm the server accepted everything. The captured wire batches are written to
``captured-audit/openai-prod-run/sdk_batches.jsonl`` for the server-side verification
(§5 of ``private/v3/live-run-verification-runbook.md``).

Uses a FRESH ``conversation_id`` each run (``RUNFILE_CONVERSATION_ID``) so the run is
first-in-conversation (genesis ``prev_event_hash = zero sentinel``) — avoiding the
documented conversation-reuse genesis ``chain_break`` false alarm. Unlike the LangGraph
version, this agent's HITL is a *blocking MCP tool* (not the SDK interruption flow), so
capture is a single segment — the approval appears as a long ``tool_call``/``tool_result``
pair, not a ``run_suspend``/``run_resume``.

Resolve the approval gate from a second terminal with the sibling project's
``scripts/officer_console.py auto``. Requires the prod test key in
``platform/tools/seed-test-tenant/.seed-secrets.local`` and a seeded mimic DB.

    .venv/bin/python -m scripts.live_run [REQUEST_ID]
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import uuid
from collections import Counter
from pathlib import Path

PROD_BASE_URL = "https://api.eu-west-2.runfile.ai"
EXAMPLE_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = EXAMPLE_ROOT.parents[1]
SECRETS = REPO_ROOT / "platform" / "tools" / "seed-test-tenant" / ".seed-secrets.local"
OUT_DIR = REPO_ROOT / "captured-audit" / "openai-prod-run"
DEMO_REQUEST_ID = "11111111-1111-1111-1111-111111111111"


def _read_api_key() -> str:
    if not SECRETS.exists():
        sys.exit(f"prod test key not found at {SECRETS}")
    for line in SECRETS.read_text().splitlines():
        m = re.match(r"^api_key\s+(\S+)", line)
        if m:
            return m.group(1)
    sys.exit("no `api_key` line in the seed secrets file")


def main() -> int:
    request_id = sys.argv[1] if len(sys.argv) > 1 else DEMO_REQUEST_ID
    api_key = _read_api_key()
    conversation_id = f"conv_oai_{uuid.uuid4()}"  # fresh -> first-in-conversation
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

    # 2. Point the example's Runfile capture at PROD with a fresh conversation id, and
    #    run the real agent (it inits the SDK, instruments Runner, launches the MCP
    #    server, and blocks on the approval gate until officer_console resolves it).
    os.environ["RUNFILE_API_KEY"] = api_key
    os.environ["RUNFILE_BASE_URL"] = PROD_BASE_URL
    os.environ["RUNFILE_CONVERSATION_ID"] = conversation_id

    from runfile_ai.integrations import openai_agents as runfile_openai

    runfile_openai._registry.reset()
    print(f"[live] shipping to {PROD_BASE_URL}  key={api_key[:12]}…  conv={conversation_id}")
    print("[live] intake → bureau/policy → score → escalate → HITL gate "
          "(resolve with: officer_console.py auto)…")

    from agent.main import run as run_agent

    asyncio.run(run_agent(request_id))  # the example flushes on exit

    # 3. Persist the captured wire batches (bodies only) for server-side verify.
    with open(batches_file, "w") as f:
        for s in shipped:
            f.write(json.dumps(s["body"]) + "\n")

    # 4. Report: did prod accept everything we shipped?
    items = [it for s in shipped for it in s["body"]["items"]]
    events = [it["event"] for it in items if it["type"] == "event"]
    events.sort(key=lambda e: (e.get("segment_index", 0), e["local_seq"]))
    kinds = Counter(e["action"]["kind"] for e in events)
    run_creates = [it for it in items if it["type"] == "run_create"]
    run_id = run_creates[0]["run"]["run_id"] if run_creates else "?"

    shipped_total = len(items)
    accepted_total = sum((s["response"] or {}).get("accepted_count", 0) for s in shipped)
    statuses = Counter(s["status"] for s in shipped)
    seqs = [e["local_seq"] for e in events if e.get("segment_index", 0) == 0]

    print(f"\n{'='*72}\nSHIPPED TO PROD — {run_id}\n{'='*72}")
    print(f"  batches: {len(shipped)}   http statuses: {dict(statuses)}")
    print(f"  items shipped: {shipped_total}   server accepted_count (sum): {accepted_total}")
    print(f"  event kinds: {dict(kinds)}")
    print(f"  seg0 local_seq: {seqs}")

    checks = {
        "all batches HTTP 200": set(statuses) == {200},
        "server accepted == shipped": accepted_total == shipped_total,
        "run_create present (genesis)": bool(run_creates),
        "run_end present": any(it["type"] == "run_end" for it in items),
        "tool_call == tool_result (blocking-tool HITL, no dangling)": kinds["tool_call"] == kinds["tool_result"],
        "at least one llm_call": kinds["llm_call"] >= 1,
        "local_seq contiguous (single segment)": seqs == list(range(len(seqs))),
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
