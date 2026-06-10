// Agent identity and model/prompt provenance.
//
// CLAUDE.md is the single source of truth for the agent's operating
// instructions — it governs both the Claude Code CLI runtime and the Agent SDK
// runtime. So the canonical PROMPT_VERSION_HASH is a sha256 over CLAUDE.md's raw
// bytes, and that exact value is what the runtime records via
// creditline_record_decision so a recorded decision pins precisely which prompt
// produced it. The MCP server exposes the same value through
// creditline_get_agent_provenance.
import { readFileSync } from "node:fs";
import { createHash } from "node:crypto";
import { fileURLToPath } from "node:url";
import { dirname, join, resolve } from "node:path";

export const AGENT_ID = "creditline-decision-agent";
export const AGENT_VERSION = "0.1.0";
export const MODEL_VERSION = process.env.AGENT_MODEL || "claude-opus-4-8";

const __dirname = dirname(fileURLToPath(import.meta.url));
const CLAUDE_MD = join(resolve(__dirname, "..", ".."), "CLAUDE.md");

const FALLBACK_PROMPT =
  "You are the Credit-Line Decision Agent. Act only through the mimic-creditline " +
  "MCP tools; intake -> bureau -> policy -> score -> decide -> record -> human " +
  "approval. Never auto-deny; every adverse outcome escalates to a human.";

function loadPrompt(): Buffer | string {
  try {
    return readFileSync(CLAUDE_MD);
  } catch {
    return FALLBACK_PROMPT;
  }
}

const PROMPT_BYTES = loadPrompt();

// The system prompt for the SDK runtime IS the CLAUDE.md content, so both
// runtimes are governed by — and pin the hash of — the same instructions.
export const SYSTEM_PROMPT = PROMPT_BYTES.toString();
export const PROMPT_VERSION_HASH = "sha256:" + createHash("sha256").update(PROMPT_BYTES).digest("hex");
