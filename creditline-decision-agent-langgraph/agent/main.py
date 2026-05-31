"""Live Credit-Line Decision Agent on LangGraph.

Loads the SAME `mimic-creditline` MCP tools used by the Claude and OpenAI builds
(via langchain-mcp-adapters' MultiServerMCPClient over stdio), wires them into a
LangGraph prebuilt ReAct agent, and runs one credit-line request end to end —
including blocking on the human-in-the-loop approval gate.

    python -m agent.main [REQUEST_ID]

Requires a model API key for the configured provider (ANTHROPIC_API_KEY by
default) and a seeded mimic_creditline database. Resolve any pending approval
from a second terminal with the sibling project's scripts/officer_console.py
(the HITL surface is shared).
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import timedelta
from pathlib import Path

from dotenv import load_dotenv

from .prompts import INSTRUCTIONS, MODEL, MODEL_PROVIDER

load_dotenv()

# The reusable environment (MCP server + DB) lives in the sibling project.
SIBLING = Path(__file__).resolve().parents[2] / "creditline-decision-agent"
DEMO_REQUEST_ID = "11111111-1111-1111-1111-111111111111"


async def run(request_id: str) -> None:
    from langchain.chat_models import init_chat_model
    from langchain_mcp_adapters.client import MultiServerMCPClient
    from langgraph.prebuilt import create_react_agent

    approval_timeout = int(os.environ.get("APPROVAL_TIMEOUT_SECONDS", "900"))
    mcp_env = {
        "MIMIC_DB_DSN": os.environ["MIMIC_DB_DSN"],
        "APPROVAL_TIMEOUT_SECONDS": str(approval_timeout),
        "APPROVAL_POLL_SECONDS": os.environ.get("APPROVAL_POLL_SECONDS", "2"),
        "PATH": os.environ.get("PATH", ""),
    }

    client = MultiServerMCPClient(
        {
            "mimic-creditline": {
                "transport": "stdio",
                "command": sys.executable,
                "args": ["-m", "mcp_servers.mimic_creditline.server"],
                "cwd": str(SIBLING),
                "env": mcp_env,
                # The MCP session must outlast the blocking approval gate, or the
                # request_approval tool call would hit the default read timeout.
                "session_kwargs": {
                    "read_timeout_seconds": timedelta(seconds=approval_timeout + 30)
                },
            }
        }
    )
    tools = await client.get_tools()

    model = init_chat_model(MODEL, model_provider=MODEL_PROVIDER, temperature=0)
    agent = create_react_agent(model, tools, prompt=INSTRUCTIONS)

    user_prompt = (
        f"Process credit-line request {request_id} end to end. When you escalate, "
        f"open the approval gate and wait for the credit officer's resolution, "
        f"then state the final outcome."
    )

    print(f"=== Credit-Line Decision Agent (LangGraph) — request {request_id} ===\n")
    result = await agent.ainvoke(
        {"messages": [{"role": "user", "content": user_prompt}]},
        config={"recursion_limit": 60},
    )
    print(result["messages"][-1].content)
    print("\n=== run complete ===")


if __name__ == "__main__":
    req = sys.argv[1] if len(sys.argv) > 1 else DEMO_REQUEST_ID
    asyncio.run(run(req))
