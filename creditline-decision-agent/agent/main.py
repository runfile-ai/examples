"""Live Credit-Line Decision Agent on the Claude Agent SDK.

Registers the `mimic-creditline` MCP server as an external stdio subprocess,
loads the project Skills (intake / scoring / decisioning), and runs one
credit-line request end to end — including blocking on the human-in-the-loop
approval gate when the decision requires it.

    python -m agent.main [REQUEST_ID]

Requires ANTHROPIC_API_KEY and a seeded mimic_creditline database. Resolve any
pending approval from a second terminal with scripts/officer_console.py.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from .prompts import MODEL_VERSION, SYSTEM_PROMPT

load_dotenv()

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEMO_REQUEST_ID = "11111111-1111-1111-1111-111111111111"

MCP_TOOLS = [
    "mcp__mimic-creditline__creditline_get_agent_provenance",
    "mcp__mimic-creditline__creditline_get_request",
    "mcp__mimic-creditline__creditline_get_customer",
    "mcp__mimic-creditline__creditline_pull_bureau",
    "mcp__mimic-creditline__creditline_get_active_policy",
    "mcp__mimic-creditline__creditline_record_decision",
    "mcp__mimic-creditline__creditline_request_approval",
    "mcp__mimic-creditline__creditline_notify_customer",
]


def build_options():
    from claude_agent_sdk import ClaudeAgentOptions

    # The MCP server runs as its own process and connects to the environment DB
    # with the least-privilege agent role. Pass through only what it needs.
    mcp_env = {
        "MIMIC_DB_DSN": os.environ["MIMIC_DB_DSN"],
        "APPROVAL_TIMEOUT_SECONDS": os.environ.get("APPROVAL_TIMEOUT_SECONDS", "900"),
        "APPROVAL_POLL_SECONDS": os.environ.get("APPROVAL_POLL_SECONDS", "2"),
        "PATH": os.environ.get("PATH", ""),
    }

    return ClaudeAgentOptions(
        model=MODEL_VERSION,
        system_prompt=SYSTEM_PROMPT,
        cwd=str(PROJECT_ROOT),
        # Load project Skills from .claude/skills and project settings.
        setting_sources=["project"],
        allowed_tools=MCP_TOOLS,
        # Non-interactive demo: don't prompt for each domain tool call.
        permission_mode="bypassPermissions",
        mcp_servers={
            "mimic-creditline": {
                "command": sys.executable,
                "args": ["-m", "mcp_servers.mimic_creditline.server"],
                "env": mcp_env,
            }
        },
    )


def _render(message) -> None:
    """Best-effort pretty-printer for streamed SDK messages."""
    content = getattr(message, "content", None)
    if content is None:
        return
    for block in content:
        btype = type(block).__name__
        text = getattr(block, "text", None)
        if text:
            print(text, end="", flush=True)
        elif "ToolUse" in btype:
            name = getattr(block, "name", "?")
            print(f"\n  → tool call: {name} {getattr(block, 'input', '')}", flush=True)
        elif "ToolResult" in btype:
            print(f"\n  ← tool result received", flush=True)


async def run(request_id: str) -> None:
    from claude_agent_sdk import query

    prompt = (
        f"Process credit-line request {request_id}. Follow the full intake → "
        f"bureau → policy → score → decide → record → human-approval flow. "
        f"When you escalate, open the approval gate and wait for the credit "
        f"officer's resolution, then state the final outcome."
    )

    print(f"=== Credit-Line Decision Agent — request {request_id} ===\n")
    async for message in query(prompt=prompt, options=build_options()):
        _render(message)
    print("\n\n=== run complete ===")


if __name__ == "__main__":
    req = sys.argv[1] if len(sys.argv) > 1 else DEMO_REQUEST_ID
    asyncio.run(run(req))
