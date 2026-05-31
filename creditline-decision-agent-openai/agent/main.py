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


async def run(request_id: str) -> None:
    from agents import Agent, ModelSettings, Runner, set_tracing_disabled
    from agents.mcp import MCPServerStdio

    # No OpenAI tracing export (keeps runs self-contained).
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
        result = await Runner.run(agent, prompt, max_turns=40)
        print(result.final_output)

    print("\n=== run complete ===")


if __name__ == "__main__":
    req = sys.argv[1] if len(sys.argv) > 1 else DEMO_REQUEST_ID
    asyncio.run(run(req))
