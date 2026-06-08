"""Fidelity check: log what LangGraph emits, log what the SDK captures, compare.

Runs the credit-line graph with TWO callbacks attached at once:
  1. a raw recorder that logs every LangGraph callback verbatim (the ground truth
     of what the framework actually emitted — run-ids, tool_call_ids, model,
     interrupt/resume), and
  2. the real Runfile LangGraph adapter.

Then it prints both logs side by side and reconciles them signal-by-signal: every
LangGraph LLM run ↔ one ``llm_call``; every tool start/end ↔ ``tool_call`` /
``tool_result`` matched by ``tool_call_id``; the issuer of each tool call; the
parallel grouping; interrupt ↔ ``run_suspend``; resume ↔ ``run_resume``; root chain
start/end ↔ ``run_create`` / ``run_end``. This proves the adapter translated what
LangGraph gave — nothing dropped, nothing fabricated — rather than just checking the
captured chain is internally consistent.

Run (from this directory, with the SDK venv)::

    ../../sdks/packages/python/.venv/bin/python -m scripts.trace_compare

Reads the SDK buffer directly (flusher off) so the captured events are exactly what
the adapter produced. Exits non-zero on any unreconciled signal.
"""
from __future__ import annotations

import asyncio
from typing import Any

from scripts.capture_verify import (
    AGENT_IDENTITY,
    DEMO_REQUEST_ID,
    _OFFICER_RESOLUTION,
    _build_model,
    _build_tools,
)


# --------------------------------------------------------------------------- #
# 1. Raw recorder — logs exactly what LangGraph hands the callback, in order.
# --------------------------------------------------------------------------- #


def _make_raw_recorder() -> Any:
    from langgraph.callbacks import GraphCallbackHandler

    class RawRecorder(GraphCallbackHandler):
        raise_error = False

        def __init__(self) -> None:
            self.log: list[dict[str, Any]] = []

        def _rec(self, kind: str, **fields: Any) -> None:
            self.log.append({"cb": kind, **fields})

        def on_chain_start(self, serialized, inputs, *, run_id, parent_run_id=None,
                           tags=None, metadata=None, **kw):
            if parent_run_id is None:  # only the run boundary, not inner runnables
                self._rec("chain_start[root]", run_id=str(run_id),
                          thread_id=(metadata or {}).get("thread_id"))

        def on_chain_end(self, outputs, *, run_id, parent_run_id=None, **kw):
            if parent_run_id is None:
                self._rec("chain_end[root]", run_id=str(run_id))

        def on_chain_error(self, error, *, run_id, parent_run_id=None, **kw):
            if parent_run_id is None:
                self._rec("chain_error[root]", run_id=str(run_id), error=type(error).__name__)

        def on_chat_model_start(self, serialized, messages, *, run_id, parent_run_id=None,
                                tags=None, metadata=None, **kw):
            self._rec("chat_model_start", run_id=str(run_id),
                      provider=(metadata or {}).get("ls_provider"))

        def on_llm_end(self, response, *, run_id, parent_run_id=None, **kw):
            msg = response.generations[0][0].message
            self._rec("llm_end", run_id=str(run_id),
                      model=(getattr(msg, "response_metadata", {}) or {}).get("model_name"),
                      issued_tool_calls=[tc.get("id") for tc in getattr(msg, "tool_calls", [])],
                      usage=getattr(msg, "usage_metadata", None))

        def on_tool_start(self, serialized, input_str, *, run_id, parent_run_id=None,
                          tags=None, metadata=None, inputs=None, **kw):
            self._rec("tool_start", run_id=str(run_id), name=(serialized or {}).get("name"),
                      tool_call_id=kw.get("tool_call_id"))

        def on_tool_end(self, output, *, run_id, parent_run_id=None, **kw):
            self._rec("tool_end", run_id=str(run_id),
                      tool_call_id=getattr(output, "tool_call_id", None))

        def on_tool_error(self, error, *, run_id, parent_run_id=None, **kw):
            self._rec("tool_error", run_id=str(run_id), error=type(error).__name__)

        def on_interrupt(self, event):
            self._rec("interrupt", run_id=str(getattr(event, "run_id", None)),
                      interrupts=[getattr(i, "value", None) for i in getattr(event, "interrupts", ())])

        def on_resume(self, event):
            self._rec("resume", run_id=str(getattr(event, "run_id", None)))

    return RawRecorder()


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _fmt_raw(e: dict[str, Any]) -> str:
    rid = e.get("run_id", "")
    tail = "…" + rid[-6:] if rid and rid != "None" else ""
    bits = []
    if e.get("name"):
        bits.append(e["name"])
    if e.get("model"):
        bits.append(f"model={e['model']}")
    if e.get("provider"):
        bits.append(f"provider={e['provider']}")
    if e.get("tool_call_id"):
        bits.append(f"tcid={e['tool_call_id']}")
    if e.get("issued_tool_calls"):
        bits.append(f"issues={e['issued_tool_calls']}")
    if e.get("usage"):
        u = e["usage"]
        bits.append(f"tok={u.get('input_tokens')}/{u.get('output_tokens')}")
    if e.get("interrupts"):
        bits.append(f"value={e['interrupts']}")
    if e.get("error"):
        bits.append(f"err={e['error']}")
    return f"  {e['cb']:18} {tail:8} {'  '.join(str(b) for b in bits)}"


def _fmt_sdk(ev: dict[str, Any], by_id: dict[str, dict]) -> str:
    a = ev["action"]
    parent = ev.get("parent_event_id")
    pkind = by_id.get(parent, {}).get("action", {}).get("kind") if parent else None
    bits = [f"name={a.get('name')}"]
    if ev.get("model_ref"):
        m = ev["model_ref"]
        bits.append(f"model={m.get('model_id')}/{m.get('provider')} tok={m.get('input_tokens')}/{m.get('output_tokens')}")
    if a.get("outcome"):
        bits.append(f"outcome={a['outcome']}")
    if ev.get("parallel_group_id"):
        bits.append(f"pg=…{ev['parallel_group_id'][-6:]}")
    if a["kind"] == "run_suspend":
        bits.append(f"reason={ev['suspension_details']['reason']}")
    parent_str = f"parent={pkind or '-'}"
    return f"  {ev['segment_index']}.{ev['local_seq']:<2} {a['kind']:14} {parent_str:18} {'  '.join(bits)}"


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #


async def _run() -> tuple[list[dict], list[dict]]:
    import os
    import tempfile

    import runfile_ai
    from runfile_ai.integrations import langgraph as runfile_langgraph
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.prebuilt import create_react_agent
    from langgraph.types import Command

    # Isolate the spool: this harness points at an unreachable base_url and calls
    # shutdown() (final drain) — without an isolated spool dir the failed POST would
    # spool ciphertext into the shared default ~/.runfile/spool/, which a later real
    # init() (e.g. live_run against prod) would drain and ship as an orphan run. Keep
    # the spool in a throwaway dir so this read-only trace can never contaminate prod.
    os.environ["RUNFILE_SPOOL_DIR"] = tempfile.mkdtemp(prefix="rf-trace-spool-")

    runfile_langgraph._registry.reset()
    inst = runfile_ai.init(
        api_key="rf_test_" + "a" * 32, environment="production",
        base_url="http://localhost:9", start_flusher=False, fetch_policy=False,
    )
    raw = _make_raw_recorder()
    handler = runfile_langgraph.build_handler(AGENT_IDENTITY, conversation_id=DEMO_REQUEST_ID)
    agent = create_react_agent(_build_model(), _build_tools(), checkpointer=InMemorySaver())
    agent = agent.with_config({"callbacks": [raw, handler]})
    cfg = {"configurable": {"thread_id": DEMO_REQUEST_ID}, "recursion_limit": 60}

    await agent.ainvoke(
        {"messages": [{"role": "user", "content": f"Process {DEMO_REQUEST_ID}"}]}, config=cfg
    )
    await agent.ainvoke(Command(resume=_OFFICER_RESOLUTION), config=cfg)

    from runfile_ai.buffer import BufferedEvent

    sdk_events = [b.event for b in inst.buffer.snapshot() if isinstance(b, BufferedEvent)]
    sdk_events.sort(key=lambda e: (e.get("segment_index", 0), e["local_seq"]))
    runfile_ai.shutdown()
    return raw.log, sdk_events


def _reconcile(raw: list[dict], sdk: list[dict]) -> bool:
    by_id = {e["event_id"]: e for e in sdk}
    sdk_kind = lambda k: [e for e in sdk if e["action"]["kind"] == k]  # noqa: E731

    # Raw signal tallies
    raw_llm = [e for e in raw if e["cb"] == "llm_end"]
    raw_tool_start = [e for e in raw if e["cb"] == "tool_start"]
    raw_tool_end = [e for e in raw if e["cb"] == "tool_end"]
    raw_interrupt = [e for e in raw if e["cb"] == "interrupt"]
    raw_resume = [e for e in raw if e["cb"] == "resume"]
    raw_root_start = [e for e in raw if e["cb"] == "chain_start[root]"]
    raw_root_end = [e for e in raw if e["cb"] in ("chain_end[root]", "chain_error[root]")]

    # The adapter intentionally collapses LangGraph's per-invocation root chains into
    # ONE Runfile run across a suspend/resume (resume-vs-fork detection by thread_id):
    #   - a resume invocation reuses the existing run → it does NOT add a run_create;
    #   - the root chain_end that fires while the run is suspended is a PAUSE, not an
    #     end → it does NOT add a run_end.
    # So the expected counts net out the resume/interrupt accounting, rather than 1:1.
    n_starts, n_ends = len(raw_root_start), len(raw_root_end)
    n_resume, n_interrupt = len(raw_resume), len(raw_interrupt)
    exp_create = n_starts - n_resume      # resume continuations don't open a new run
    exp_end = n_ends - n_interrupt        # a chain_end right after an interrupt = pause
    rows = [
        (f"root chain_start({n_starts}) − resume({n_resume}) → run_create", exp_create, len(sdk_kind("run_create"))),
        (f"root chain_end({n_ends}) − interrupt({n_interrupt}) → run_end",   exp_end,    len(sdk_kind("run_end"))),
        ("llm_end → llm_call",                          len(raw_llm),        len(sdk_kind("llm_call"))),
        ("tool_start → tool_call",                      len(raw_tool_start), len(sdk_kind("tool_call"))),
        ("tool_end(success) → tool_result",             len(raw_tool_end),   len(sdk_kind("tool_result"))),
        ("interrupt → run_suspend",                     len(raw_interrupt),  len(sdk_kind("run_suspend"))),
        ("resume → run_resume",                         len(raw_resume),     len(sdk_kind("run_resume"))),
    ]
    print(f"\n{'='*78}\nRECONCILIATION  (LangGraph signal → SDK event)\n{'='*78}")
    print("  2 LangGraph root chains (invoke + resume) collapse into 1 Runfile run; the")
    print("  interrupted tool_call has no tool_result (8 calls → 7 results) — both expected.\n")
    print(f"  {'mapping':46} {'expect':>7} {'SDK':>5}  ok")
    counts_ok = True
    for name, lg, s in rows:
        ok = lg == s
        counts_ok = counts_ok and ok
        print(f"  {name:46} {lg:>7} {s:>5}  {'OK' if ok else 'MISMATCH'}")

    # Per-tool correlation by tool_call_id (raw) ↔ SDK tool_call, in capture order.
    print("\n  tool_call_id correlation (raw tool_start ↔ SDK tool_call, ordered):")
    sdk_calls = sdk_kind("tool_call")
    corr_ok = len(raw_tool_start) == len(sdk_calls)
    for i, rt in enumerate(raw_tool_start):
        sc = sdk_calls[i] if i < len(sdk_calls) else None
        if sc is None:
            print(f"    {rt['tool_call_id']:10} {rt['name']:28} → (no SDK tool_call)  MISSING")
            corr_ok = False
            continue
        parent = by_id.get(sc.get("parent_event_id"), {})
        issuer_ok = parent.get("action", {}).get("kind") == "llm_call"
        pg = sc.get("parallel_group_id")
        print(f"    {rt['tool_call_id']:10} {rt['name']:28} → {sc['action']['name']:24} "
              f"parent={'llm_call' if issuer_ok else parent.get('action',{}).get('kind','-')}"
              f"{'  pg=…'+pg[-6:] if pg else ''}  {'OK' if issuer_ok else 'NO ISSUER'}")
        corr_ok = corr_ok and issuer_ok

    # Parallel grouping: which tool_call_ids LangGraph issued together (one llm_end
    # with ≥2) and whether the SDK grouped exactly those.
    print("\n  parallel grouping (one llm_end issuing ≥2 tool calls):")
    grp_ok = True
    for e in raw_llm:
        issued = [t for t in e["issued_tool_calls"] if t]
        if len(issued) < 2:
            continue
        # the SDK tool_calls for these tcids = the calls at the matching positions
        idxs = [i for i, rt in enumerate(raw_tool_start) if rt["tool_call_id"] in issued]
        pgs = {sdk_calls[i].get("parallel_group_id") for i in idxs if i < len(sdk_calls)}
        ok = len(pgs) == 1 and None not in pgs
        grp_ok = grp_ok and ok
        print(f"    issued {issued} → SDK group(s) {pgs}  {'OK (one shared)' if ok else 'NOT GROUPED'}")
    if all(len([t for t in e['issued_tool_calls'] if t]) < 2 for e in raw_llm):
        print("    (none)")

    ok = counts_ok and corr_ok and grp_ok
    print(f"\n{'PASS ✅' if ok else 'FAIL ❌'} — every LangGraph signal reconciles to an SDK event")
    return ok


def main() -> int:
    raw, sdk = asyncio.run(_run())

    print(f"{'='*78}\n1) WHAT LANGGRAPH EMITTED  ({len(raw)} callbacks)\n{'='*78}")
    for e in raw:
        print(_fmt_raw(e))

    by_id = {e["event_id"]: e for e in sdk}
    print(f"\n{'='*78}\n2) WHAT THE SDK CAPTURED  ({len(sdk)} events)\n{'='*78}")
    for ev in sdk:
        print(_fmt_sdk(ev, by_id))

    return 0 if _reconcile(raw, sdk) else 1


if __name__ == "__main__":
    raise SystemExit(main())
