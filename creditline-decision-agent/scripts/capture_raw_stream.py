"""Capture the RAW claude_agent_sdk.query() message stream to JSONL.

Runs the same agent/options as agent.main but taps query() directly (no runfile
adapter) and dumps every message + every content block with ALL fields, so we
can see exactly how the CLI streams a single model turn (message_id, uuid,
stop_reason, parent_tool_use_id, usage) and decide the correct coalescing key.

    python -m scripts.capture_raw_stream  > /dev/null
    # writes scripts/raw-stream.jsonl  (resolve the gate with officer_console auto)
"""
from __future__ import annotations
import asyncio, dataclasses, json, sys
from pathlib import Path
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from agent.main import build_options, DEMO_REQUEST_ID  # reuse the real options

load_dotenv()
OUT = Path(__file__).resolve().parent / "raw-stream.jsonl"


def block_to_dict(b):
    d = {"__type__": type(b).__name__}
    if dataclasses.is_dataclass(b):
        for f in dataclasses.fields(b):
            v = getattr(b, f.name, None)
            # truncate big text/inputs so the dump stays readable
            if isinstance(v, str) and len(v) > 200:
                v = v[:200] + f"…(+{len(v)-200})"
            d[f.name] = v
    return d


def msg_to_dict(m):
    d = {"__type__": type(m).__name__}
    if dataclasses.is_dataclass(m):
        for f in dataclasses.fields(m):
            v = getattr(m, f.name, None)
            if f.name == "content" and isinstance(v, list):
                d["content"] = [block_to_dict(b) for b in v]
            else:
                d[f.name] = v
    else:
        d["repr"] = repr(m)[:300]
    return d


async def main(request_id: str) -> None:
    from claude_agent_sdk import query
    prompt = (
        f"Process credit-line request {request_id}. Follow the full intake → "
        f"bureau → policy → score → decide → record → human-approval flow. "
        f"When you escalate, open the approval gate and wait for the credit "
        f"officer's resolution, then state the final outcome."
    )
    n = 0
    with OUT.open("w") as fh:
        async for message in query(prompt=prompt, options=build_options()):
            n += 1
            rec = msg_to_dict(message)
            rec["_seq"] = n
            fh.write(json.dumps(rec, default=str) + "\n")
            fh.flush()
            t = rec["__type__"]
            mid = rec.get("message_id"); sr = rec.get("stop_reason")
            blocks = [b.get("__type__") for b in rec.get("content", [])] if isinstance(rec.get("content"), list) else []
            print(f"{n:3d} {t:18s} msg_id={mid} stop={sr} blocks={blocks}", file=sys.stderr, flush=True)
    print(f"\nwrote {n} messages to {OUT}", file=sys.stderr)


if __name__ == "__main__":
    req = sys.argv[1] if len(sys.argv) > 1 else DEMO_REQUEST_ID
    asyncio.run(main(req))
