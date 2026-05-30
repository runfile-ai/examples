"""Agent identity and model/prompt provenance.

CLAUDE.md is the single source of truth for the agent's operating instructions —
it governs both the Claude Code CLI runtime and the Agent SDK runtime. So the
canonical ``PROMPT_VERSION_HASH`` is a sha256 over CLAUDE.md's raw bytes, and
that exact value is what every runtime records via ``creditline_record_decision``
so a recorded decision pins precisely which prompt produced it.

The MCP server exposes the same value through ``creditline_get_agent_provenance``
(see ``prompt_version_hash`` there) so a CLI agent that never imports this module
still records the canonical hash rather than a fallback.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

AGENT_ID = "creditline-decision-agent"
AGENT_VERSION = "0.1.0"
MODEL_VERSION = os.environ.get("AGENT_MODEL", "claude-opus-4-8")

_CLAUDE_MD = Path(__file__).resolve().parent.parent / "CLAUDE.md"

_FALLBACK_PROMPT = (
    "You are the Credit-Line Decision Agent. Act only through the mimic-creditline "
    "MCP tools; intake -> bureau -> policy -> score -> decide -> record -> human "
    "approval. Never auto-deny; every adverse outcome escalates to a human."
)


def _load_prompt() -> bytes:
    try:
        return _CLAUDE_MD.read_bytes()
    except OSError:
        return _FALLBACK_PROMPT.encode("utf-8")


_PROMPT_BYTES = _load_prompt()

# The system prompt for the SDK runtime IS the CLAUDE.md content, so both
# runtimes are governed by — and pin the hash of — the same instructions.
SYSTEM_PROMPT = _PROMPT_BYTES.decode("utf-8")
PROMPT_VERSION_HASH = "sha256:" + hashlib.sha256(_PROMPT_BYTES).hexdigest()
