"""Live Credit-Line Decision Agent on the OpenAI Agents SDK.

Connects to the SAME `mimic-creditline` MCP server used by the Claude version
(launched as a stdio subprocess from the sibling project) and runs one
credit-line request end to end, including blocking on the human-in-the-loop
approval gate.

    python -m agent.main [REQUEST_ID]

Requires OPENAI_API_KEY and a seeded mimic_creditline database. Resolve any
pending approval from a second terminal with the sibling project's
scripts/officer_console.py (the HITL surface is shared).
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from .prompts import INSTRUCTIONS, MODEL_VERSION

load_dotenv()

# The reusable environment (MCP server + DB) lives in the sibling project.
SIBLING = Path(__file__).resolve().parents[2] / "creditline-decision-agent"
DEMO_REQUEST_ID = "11111111-1111-1111-1111-111111111111"


RUNFILE_AGENT_IDENTITY = "did:web:runfile.ai:agents:creditline-decision-agent-openai:0.1.0"


def _instrument_runfile(runner: object) -> tuple[object, bool]:
    """Wrap the ``Runner`` with the Runfile OpenAI Agents adapter, if configured.

    Returns ``(runner, capturing)``: the wrapped runner + whether capture is on. A no-op
    (returns the runner unchanged, ``capturing=False``) when ``runfile-ai`` isn't
    installed or ``RUNFILE_API_KEY`` isn't set, so the example runs identically without
    Runfile. ``RUNFILE_BASE_URL`` points the SDK at a local ingest stand-in for offline
    capture. The adapter registers a tracing processor (``set_trace_processors``), so —
    unlike the un-instrumented path — tracing must stay ENABLED when capturing.
    """
    if not os.environ.get("RUNFILE_API_KEY"):
        return runner, False
    try:
        import runfile_ai
        from runfile_ai.integrations import openai_agents as runfile_openai
    except ImportError:
        print("[runfile] RUNFILE_API_KEY set but runfile-ai not installed — skipping capture.")
        return runner, False

    init_kwargs = {"api_key": os.environ["RUNFILE_API_KEY"]}
    if os.environ.get("RUNFILE_BASE_URL"):
        init_kwargs["base_url"] = os.environ["RUNFILE_BASE_URL"]
    runfile_ai.init(**init_kwargs)
    print(f"[runfile] capturing this run as {RUNFILE_AGENT_IDENTITY}")
    wrapped = runfile_openai.instrument_runner(
        runner,
        agent_identity=RUNFILE_AGENT_IDENTITY,
        # RUNFILE_CONVERSATION_ID lets a verification run use a fresh, first-in-conversation
        # id (clean genesis hash); defaults to the demo request id.
        conversation_id=os.environ.get("RUNFILE_CONVERSATION_ID", DEMO_REQUEST_ID),
    )
    return wrapped, True


async def run(request_id: str) -> None:
    from agents import Agent, ModelSettings, Runner, set_tracing_disabled
    from agents.mcp import MCPServerStdio

    runner, capturing = _instrument_runfile(Runner)
    if not capturing:
        # No Runfile capture → no OpenAI tracing export either (keeps runs
        # self-contained). When capturing, tracing stays on for the Runfile processor.
        set_tracing_disabled(True)

    approval_timeout = int(os.environ.get("APPROVAL_TIMEOUT_SECONDS", "900"))
    mcp_env = {
        "MIMIC_DB_DSN": os.environ["MIMIC_DB_DSN"],
        "APPROVAL_TIMEOUT_SECONDS": str(approval_timeout),
        "APPROVAL_POLL_SECONDS": os.environ.get("APPROVAL_POLL_SECONDS", "2"),
        "PATH": os.environ.get("PATH", ""),
    }

    print(f"=== Credit-Line Decision Agent (OpenAI Agents SDK) — request {request_id} ===\n")

    async with MCPServerStdio(
        name="mimic-creditline",
        params={
            "command": sys.executable,
            "args": ["-m", "mcp_servers.mimic_creditline.server"],
            "cwd": str(SIBLING),
            "env": mcp_env,
        },
        cache_tools_list=True,
        # Must outlast the blocking approval gate, or the tool call would time out.
        client_session_timeout_seconds=float(approval_timeout + 30),
    ) as server:
        agent = Agent(
            name="Credit-Line Decision Agent",
            instructions=INSTRUCTIONS,
            model=MODEL_VERSION,
            mcp_servers=[server],
            model_settings=ModelSettings(temperature=0),
        )
        prompt = (
            f"Process credit-line request {request_id} end to end. When you "
            f"escalate, open the approval gate and wait for the credit officer's "
            f"resolution, then state the final outcome."
        )
        result = await runner.run(agent, prompt, max_turns=40)
        print(result.final_output)

    if capturing:
        import runfile_ai

        runfile_ai.flush()  # ship the captured batch before exit

    print("\n=== run complete ===")


if __name__ == "__main__":
    req = sys.argv[1] if len(sys.argv) > 1 else DEMO_REQUEST_ID
    asyncio.run(run(req))
