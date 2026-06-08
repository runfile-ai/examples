"""End-to-end verification of the Runfile LangGraph adapter against this example.

Runs the example's *graph shape* (``create_react_agent`` + the credit-line tool
sequence + the HITL escalation) instrumented with the real Runfile LangGraph
adapter, through the **real** SDK pipeline (buffer → background flusher → redact →
AES-256-GCM encrypt → ``POST /v1/batches``) into the repo's local ingest stand-in
(``captured-audit/fake_ingest.py``), then verifies the captured batches with
``captured-audit/verify_audit.py`` (recomputes the hash chain via the SDK's own
canonical projection).

Why a scripted model + in-process tools instead of ``agent/main.py``: the live
agent needs an ``ANTHROPIC_API_KEY`` and a seeded Postgres for the MCP server,
which aren't available in every environment. Everything *under test* is real — the
adapter, the SDK flusher/redaction/encryption, the ingest contract, and the
verifier. Only the model and the tool data are stubbed (faithfully reproducing the
demo: Dana Whitfield, escalate → officer override → approve 12,000). ``agent/main.py``
is instrumented with the same one-line ``instrument()`` for live capture when a key
is present.

Run (from this directory, using the SDK's venv which has runfile-ai + langgraph)::

    ../../sdks/packages/python/.venv/bin/python -m scripts.capture_verify

Exits non-zero if verification fails. Writes the captured batch log to
``captured-audit/langgraph-run/batches.jsonl`` at the repo root.
"""
from __future__ import annotations

import importlib.util
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any

EXAMPLE_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = EXAMPLE_ROOT.parents[1]
CAPTURED_AUDIT = REPO_ROOT / "captured-audit"
OUT_DIR = CAPTURED_AUDIT / "langgraph-run"

sys.path.insert(0, str(EXAMPLE_ROOT))  # so `agent.*` (real decision logic) imports

DEMO_REQUEST_ID = "11111111-1111-1111-1111-111111111111"
AGENT_IDENTITY = "did:web:runfile.ai:agents:creditline-decision-agent-langgraph:0.1.0"


def _load_module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, str(path))
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── seeded credit-line data (mirrors the deterministic demo's escalation case) ──
_CUSTOMER = {
    "customer_id": "cust-dana",
    "full_name": "Dana Whitfield",  # person_name → tokenized by the harness policy
    "email": "dana.whitfield@example.com",  # email_address → hashed
    "date_of_birth": "1987-04-02",  # dob → dropped
    "annual_income": 80000,
}
_REQUEST = {
    "request_id": DEMO_REQUEST_ID,
    "customer_id": "cust-dana",
    "request_type": "limit_increase",
    "requested_limit": 25000,
    "submitted_at": "2026-06-01T09:03:02Z",
}
_BUREAU = {
    "bureau_report_id": "bureau-7781",
    "credit_score": 712,
    "total_outstanding_debt": 20000,
    "delinquencies_24m": 1,
}
_POLICY = {
    "version": "creditline-policy-2026.05",
    "thresholds": {
        "min_credit_score": 680,
        "max_dti": 0.45,
        "auto_approve_ceiling": 10000,
        "max_delinquencies_24m": 2,
    },
}
# The credit officer's resolution that resumes the HITL gate (Art. 14 override).
_OFFICER_RESOLUTION = {
    "status": "modified",
    "is_override": True,
    "modified_limit": 12000,
    "approver_id": "co-114-jmalik",
    "approver_role": "senior_officer",
}


def _build_tools() -> list[Any]:
    from decimal import Decimal

    from langchain_core.tools import tool
    from langgraph.types import interrupt

    from agent.decision import evaluate  # the example's real decision rule set

    @tool
    def creditline_get_request(request_id: str) -> dict:
        """Fetch the credit-line request."""
        return _REQUEST

    @tool
    def creditline_get_customer(customer_id: str) -> dict:
        """Fetch the customer record (contains PII → exercises redaction)."""
        return {"customer": _CUSTOMER}

    @tool
    def creditline_pull_bureau(customer_id: str) -> dict:
        """Pull the credit bureau report."""
        return _BUREAU

    @tool
    def creditline_get_active_policy() -> dict:
        """Fetch the active lending policy."""
        return _POLICY

    @tool
    def creditline_record_decision(
        request_id: str,
        outcome: str,
        rationale: str,
        model_version: str,
        prompt_version_hash: str,
        policy_version: str,
        bureau_report_id: str,
    ) -> dict:
        """Record the decision; returns whether a human gate is required."""
        rec = evaluate(
            requested_limit=Decimal(str(_REQUEST["requested_limit"])),
            annual_income=Decimal(str(_CUSTOMER["annual_income"])),
            bureau=_BUREAU,
            policy_thresholds=_POLICY["thresholds"],
        )
        return {
            "decision_id": "dec-0007",
            "outcome": rec.outcome,
            "requires_human_approval": rec.requires_human_approval,
        }

    @tool
    def creditline_request_approval(decision_id: str, summary: str) -> dict:
        """Open the human-in-the-loop approval gate. BLOCKS via interrupt()."""
        # interrupt() suspends the graph; the value returned here is whatever the
        # resuming Command(resume=...) supplies — the officer's resolution.
        return interrupt({"decision_id": decision_id, "summary": summary})

    @tool
    def creditline_notify_customer(request_id: str, outcome: str, approved_limit: int) -> dict:
        """Send the decision letter to the customer."""
        return {"sent": True, "approved_limit": approved_limit}

    return [
        creditline_get_request,
        creditline_get_customer,
        creditline_pull_bureau,
        creditline_get_active_policy,
        creditline_record_decision,
        creditline_request_approval,
        creditline_notify_customer,
    ]


def _build_model() -> Any:
    """A scripted chat model that walks the credit-line tool sequence in order.

    Driven by the count of prior assistant turns so it deterministically reproduces
    intake → bureau+policy (parallel) → record → HITL gate → (resume) → notify →
    final, exercising sequential AND parallel tool calls plus the interrupt/resume.
    """
    from langchain_core.language_models.chat_models import BaseChatModel
    from langchain_core.messages import AIMessage
    from langchain_core.outputs import ChatGeneration, ChatResult

    from agent.prompts import MODEL_VERSION, PROMPT_VERSION_HASH

    prov = {"model_version": MODEL_VERSION, "prompt_version_hash": PROMPT_VERSION_HASH}

    script: list[dict[str, Any]] = [
        {"content": "Intake: fetch the request.",
         "tool_calls": [{"name": "creditline_get_request", "args": {"request_id": DEMO_REQUEST_ID}, "id": "tc_req"}]},
        {"content": "Fetch the customer.",
         "tool_calls": [{"name": "creditline_get_customer", "args": {"customer_id": "cust-dana"}, "id": "tc_cust"}]},
        {"content": "Pull bureau and policy in parallel.",
         "tool_calls": [
             {"name": "creditline_pull_bureau", "args": {"customer_id": "cust-dana"}, "id": "tc_bureau"},
             {"name": "creditline_get_active_policy", "args": {}, "id": "tc_policy"},
         ]},
        {"content": "Score → above ceiling → escalate. Record the decision.",
         "tool_calls": [{"name": "creditline_record_decision", "args": {
             "request_id": DEMO_REQUEST_ID, "outcome": "escalated",
             "rationale": "requested 25000 > auto_approve_ceiling 10000 (large exposure)",
             "model_version": prov["model_version"], "prompt_version_hash": prov["prompt_version_hash"],
             "policy_version": _POLICY["version"], "bureau_report_id": _BUREAU["bureau_report_id"],
         }, "id": "tc_record"}]},
        {"content": "Requires human approval — open the gate.",
         "tool_calls": [{"name": "creditline_request_approval", "args": {
             "decision_id": "dec-0007", "summary": "Dana Whitfield requests 25000; above ceiling. Recommend escalate."},
             "id": "tc_approve"}]},
        # turn 5 (after resume): officer modified to 12,000 → notify
        {"content": "Officer approved a modified 12,000 limit — notify the customer.",
         "tool_calls": [{"name": "creditline_notify_customer", "args": {
             "request_id": DEMO_REQUEST_ID, "outcome": "approved", "approved_limit": 12000}, "id": "tc_notify"}]},
        {"content": "Final: approved at a modified limit of 12,000 after credit-officer override."},
    ]

    class ScriptedModel(BaseChatModel):
        @property
        def _llm_type(self) -> str:
            return "scripted-creditline"

        def bind_tools(self, *a: Any, **k: Any) -> "BaseChatModel":
            return self

        def _generate(self, messages: Any, stop: Any = None, run_manager: Any = None, **kw: Any) -> ChatResult:
            turn = sum(1 for m in messages if isinstance(m, AIMessage))
            spec = script[min(turn, len(script) - 1)]
            msg = AIMessage(
                content=spec.get("content", ""),
                tool_calls=spec.get("tool_calls", []),
                usage_metadata={"input_tokens": 800 + turn, "output_tokens": 60 + turn, "total_tokens": 860 + 2 * turn},
            )
            msg.response_metadata = {"model_name": "claude-sonnet-4-6", "model_provider": "anthropic"}
            return ChatResult(generations=[ChatGeneration(message=msg)])

    return ScriptedModel()


def main() -> int:
    # 1. Start the real local ingest stand-in, writing batches to the artifact path.
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    batches_file = OUT_DIR / "batches.jsonl"
    fake_ingest = _load_module("rf_fake_ingest", CAPTURED_AUDIT / "fake_ingest.py")
    fake_ingest.BATCHES_FILE = str(batches_file)
    open(batches_file, "w").close()  # truncate
    server = ThreadingHTTPServer(("127.0.0.1", 0), fake_ingest.Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base_url = f"http://127.0.0.1:{port}"
    print(f"[capture] ingest stand-in on {base_url} → {batches_file}")

    # 2. Init the SDK against it (real flusher + real redaction policy fetch).
    import runfile_ai
    from runfile_ai.integrations import langgraph as runfile_langgraph

    runfile_ai.init(api_key="rf_test_" + "a" * 32, environment="production", base_url=base_url)

    # 3. Build the example's graph shape, instrument it, run the credit-line flow.
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.prebuilt import create_react_agent
    from langgraph.types import Command

    agent = create_react_agent(_build_model(), _build_tools(), checkpointer=InMemorySaver())
    agent = runfile_langgraph.instrument(
        agent, agent_identity=AGENT_IDENTITY, conversation_id=DEMO_REQUEST_ID
    )
    cfg = {"configurable": {"thread_id": DEMO_REQUEST_ID}, "recursion_limit": 60}

    import asyncio

    async def drive() -> None:
        print("[capture] running agent (intake → bureau/policy → score → escalate → HITL gate)…")
        await agent.ainvoke(
            {"messages": [{"role": "user", "content": f"Process credit-line request {DEMO_REQUEST_ID} end to end."}]},
            config=cfg,
        )
        print("[capture] HITL gate hit; resuming with the officer override (modify → 12,000)…")
        await agent.ainvoke(Command(resume=_OFFICER_RESOLUTION), config=cfg)

    asyncio.run(drive())

    # 4. Drain the buffer through the flusher (ship every batch) and stop.
    runfile_ai.flush()
    runfile_ai.shutdown()
    server.shutdown()
    print(f"[capture] done. captured → {batches_file}\n")

    # 5. Verify the captured batches independently.
    verifier = _load_module("rf_verify_audit", CAPTURED_AUDIT / "verify_audit.py")
    ok = verifier.verify(str(batches_file))

    # 6. Adapter-specific assertions on top of the structural verifier.
    import json

    items = [it for line in open(batches_file) if line.strip() for it in json.loads(line)["items"]]
    kinds = [it["event"]["action"]["kind"] for it in items if it["type"] == "event"]
    frameworks = {
        it["event"]["sdk"]["framework"] for it in items if it["type"] == "event"
    } | {it["run"]["sdk_at_start"]["framework"] for it in items if it["type"] == "run_create"}
    run_ids = {it["event"]["run_id"] for it in items if it["type"] == "event"}
    updates = [it["lifecycle_state"] for it in items if it["type"] == "run_update"]
    ends = [it for it in items if it["type"] == "run_end"]

    checks = {
        "framework == {langgraph}": frameworks == {"langgraph"},
        "single run (resume did not fork)": len(run_ids) == 1,
        "run_create + run_end present": any(k == "run_create" for k in kinds) and bool(ends),
        "llm_call captured": kinds.count("llm_call") >= 2,
        "tool_call/tool_result captured": "tool_call" in kinds and "tool_result" in kinds,
        "HITL suspend+resume captured": "run_suspend" in kinds and "run_resume" in kinds,
        "lifecycle awaiting_human → active": updates == ["awaiting_human", "active"],
        "run ended success": bool(ends) and ends[-1]["outcome"] == "success",
        "redaction applied (PII)": any(
            it["type"] == "event" and it["event"].get("payload_ref", {}).get("redaction_applied")
            for it in items
        ),
    }
    print(f"\n{'='*70}\nAdapter assertions\n{'='*70}")
    for name, passed in checks.items():
        print(f"   {'OK  ' if passed else 'FAIL'} {name}")

    all_ok = ok and all(checks.values())
    print(f"\n{'PASS ✅' if all_ok else 'FAIL ❌'} — LangGraph adapter end-to-end "
          f"({len(items)} items, kinds: {', '.join(dict.fromkeys(kinds))})")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
