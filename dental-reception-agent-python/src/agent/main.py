"""Live dental reception agent on the Claude Agent SDK (Python).

Registers the `dental` MCP server (stdio subprocess), loads the project Skills,
and runs one reception session. Schedule/record writes block on the human
approval gate inside the MCP tools — resolve them from another terminal with
`python -m src.scripts.frontdesk_console`.

  python -m src.agent.main                 (uses the default session prompt)
  python -m src.agent.main "..."           (custom caller request)

Uses the Claude Code runtime, so it runs with the local `claude` login or an
ANTHROPIC_API_KEY. A seeded `dental` database must exist.
"""
from __future__ import annotations

import asyncio
import os
import sys
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    PermissionResultAllow,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
    query,
)
from dotenv import load_dotenv

from .prompts import MODEL, SYSTEM_PROMPT

load_dotenv()

PROJECT_ROOT = Path(__file__).resolve().parents[2]

MCP_TOOLS = [
    f"mcp__dental__{t}"
    for t in [
        "find_patient",
        "verify_identity",
        "get_patient_summary",
        "check_coverage",
        "get_appointments",
        "list_appointment_types",
        "find_open_slots",
        "find_recalls",
        "halo_fetch",
        "halo_fetch_many",
        "hold_slot",
        "book_appointment",
        "reschedule",
        "cancel",
        "update_contact",
        "add_to_waitlist",
        "confirm_appointment",
    ]
]


async def _allow_all(tool_name, tool_input, context):  # noqa: ANN001
    # Approve tool use programmatically (no interactive prompt). The real safety
    # gates are the identity check and the human approval queue inside the MCP writes.
    return PermissionResultAllow(behavior="allow", updated_input=tool_input)


async def _single_prompt(text: str) -> AsyncIterator[dict[str, Any]]:
    # can_use_tool requires streaming input mode, so deliver the one user turn as
    # an async iterable rather than a bare string.
    yield {"type": "user", "session_id": "", "message": {"role": "user", "content": text}}


async def run(user_prompt: str) -> None:
    session_id = os.environ.get("AGENT_SESSION_ID") or str(uuid.uuid4())
    print(f"=== Dental reception agent (Claude Agent SDK) — session {session_id} ===\n")

    options = ClaudeAgentOptions(
        model=MODEL,
        system_prompt=SYSTEM_PROMPT,
        cwd=str(PROJECT_ROOT),
        setting_sources=["project"],  # load .claude/skills + project settings
        allowed_tools=[*MCP_TOOLS, "Skill"],
        can_use_tool=_allow_all,
        mcp_servers={
            "dental": {
                "type": "stdio",
                "command": sys.executable,
                "args": ["-m", "src.mcp_server.server"],
                "env": {
                    **os.environ,
                    "AGENT_SESSION_ID": session_id,
                    "DENTAL_CHANNEL": os.environ.get("DENTAL_CHANNEL", "voice"),
                    "PYTHONPATH": str(PROJECT_ROOT),
                },
            }
        },
    )

    async for message in query(prompt=_single_prompt(user_prompt), options=options):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    sys.stdout.write(block.text)
                elif isinstance(block, ToolUseBlock):
                    sys.stdout.write(f"\n  → {block.name}({block.input})\n")
            sys.stdout.flush()
        elif isinstance(message, ResultMessage):
            cost = message.total_cost_usd
            suffix = f" (${cost:.4f})" if cost is not None else ""
            print(f"\n\n=== session complete{suffix} ===")


DEFAULT_PROMPT = (
    'A caller says: "Hi, this is Maria Garcia, 415-555-0142. I\'d like to book a cleaning, '
    "preferably a morning slot with Dr. Nguyen sometime next week.\" Verify her identity (DOB "
    "1989-04-12), check her coverage for the cleaning, find a matching slot, hold it, and book it "
    "(the booking waits for the front-desk confirmation). State the copay and the confirmed time."
)


def _main() -> None:
    prompt = " ".join(sys.argv[1:]) or DEFAULT_PROMPT
    asyncio.run(run(prompt))


if __name__ == "__main__":
    _main()
