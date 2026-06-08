"""Live claim adjudication agent on the Claude Agent SDK (Python).

Registers the `claims` MCP server (stdio subprocess), loads the project Skills,
and adjudicates one claim. Denials, reductions, pends, and large claims block on
the human reviewer gate inside post_adjudication — resolve them from another
terminal with `python -m src.scripts.reviewer_console`.

  python -m src.agent.main                  (adjudicates the default claim)
  python -m src.agent.main clm_1001         (adjudicate a specific claim id)

Uses the Claude Code runtime, so it runs with the local `claude` login or an
ANTHROPIC_API_KEY. A seeded `claims` database must exist.
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
    f"mcp__claims__{t}"
    for t in [
        "get_claim", "get_member_coverage", "get_benefit_rules", "get_accumulators",
        "get_claim_history", "check_network", "get_allowed_amount", "lookup_reason_code",
        "adjudicate_line", "record_decision", "pend_claim", "post_adjudication",
        "halo_fetch", "halo_fetch_many", "halo_verify",
    ]
]


async def _allow_all(tool_name, tool_input, context):  # noqa: ANN001
    # Approve tool use programmatically. The real gate is the human reviewer queue
    # inside post_adjudication; the engine owns the money regardless.
    return PermissionResultAllow(behavior="allow", updated_input=tool_input)


async def _single_prompt(text: str) -> AsyncIterator[dict[str, Any]]:
    # can_use_tool requires streaming input mode, so deliver the one user turn as
    # an async iterable rather than a bare string.
    yield {"type": "user", "session_id": "", "message": {"role": "user", "content": text}}


async def run(claim_id: str) -> None:
    session_id = os.environ.get("AGENT_SESSION_ID") or str(uuid.uuid4())
    print(f"=== Claim adjudication agent (Claude Agent SDK) — session {session_id} ===\n")

    user_prompt = (
        f"Adjudicate claim {claim_id}. Validate the member's eligibility and the provider network, "
        f"then for each service line gather the benefit rule, accumulators, allowed amount, and any "
        f"frequency/duplicate history (exclude this claim id), call adjudicate_line for the numbers, "
        f"and select the reason codes it suggests. Record the decisions with evidence handles, then "
        f"post the adjudication (denials/reductions/pends wait for the human reviewer). Finish with a "
        f"short EOB-style summary per line: decision, plan paid, patient responsibility, and CARC."
    )

    options = ClaudeAgentOptions(
        model=MODEL,
        system_prompt=SYSTEM_PROMPT,
        cwd=str(PROJECT_ROOT),
        setting_sources=["project"],
        allowed_tools=[*MCP_TOOLS, "Skill"],
        can_use_tool=_allow_all,
        mcp_servers={
            "claims": {
                "type": "stdio",
                "command": sys.executable,
                "args": ["-m", "src.mcp_server.server"],
                "env": {
                    **os.environ,
                    "AGENT_SESSION_ID": session_id,
                    "CLAIMS_SESSION_CLAIM_ID": claim_id,
                    "CLAIMS_CHANNEL": os.environ.get("CLAIMS_CHANNEL", "queue"),
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


def _main() -> None:
    claim_id = sys.argv[1] if len(sys.argv) > 1 else "clm_1001"
    asyncio.run(run(claim_id))


if __name__ == "__main__":
    _main()
