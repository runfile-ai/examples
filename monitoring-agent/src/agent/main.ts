// ============================================================================
// Live monitoring agent on the Claude Agent SDK (TypeScript).
//
// Registers the `monitoring` MCP server (stdio subprocess), loads the project
// Skills, and runs one monitoring session. Real-world writes block on the
// human approval gate inside the MCP tools — resolve them from another terminal
// with `npm run oncall`.
//
//   npm run agent            (uses the default session prompt)
//   npm run agent -- "..."   (custom instruction)
//
// Uses the Claude Code runtime, so it runs with the local `claude` login or an
// ANTHROPIC_API_KEY. A seeded `monitoring` database must exist.
// ============================================================================
import { fileURLToPath } from "node:url";
import { dirname, join, resolve } from "node:path";
import { randomUUID } from "node:crypto";
import { query } from "@anthropic-ai/claude-agent-sdk";
import { SYSTEM_PROMPT, MODEL } from "./prompts.js";

try { process.loadEnvFile(); } catch { /* env may come from the shell */ }

const __dirname = dirname(fileURLToPath(import.meta.url));
const PROJECT_ROOT = resolve(__dirname, "..", "..");
const SERVER_PATH = join(PROJECT_ROOT, "src", "mcp", "server.ts");
const TSX_BIN = join(PROJECT_ROOT, "node_modules", ".bin", "tsx");

const MCP_TOOLS = [
  "list_open_issues",
  "get_issue_detail",
  "search_logs",
  "list_incidents",
  "halo_fetch",
  "halo_fetch_many",
  "triage_note",
  "declare_incident",
  "acknowledge_incident",
  "resolve_incident",
  "assign_incident",
].map((t) => `mcp__monitoring__${t}`);

async function run(userPrompt: string) {
  const sessionId = process.env.AGENT_SESSION_ID || randomUUID();
  console.log(`=== Monitoring agent (Claude Agent SDK) — session ${sessionId} ===\n`);

  const q = query({
    prompt: userPrompt,
    options: {
      model: MODEL,
      systemPrompt: SYSTEM_PROMPT,
      cwd: PROJECT_ROOT,
      settingSources: ["project"], // load .claude/skills + project settings
      allowedTools: [...MCP_TOOLS, "Skill"],
      // Approve tool use programmatically (no interactive prompt, and avoids
      // --dangerously-skip-permissions which the CLI refuses under root). The
      // real safety gate is the human approval queue inside the MCP writes.
      canUseTool: async (_toolName, input) => ({ behavior: "allow", updatedInput: input }),
      mcpServers: {
        monitoring: {
          type: "stdio",
          command: TSX_BIN,
          args: [SERVER_PATH],
          env: {
            ...process.env,
            AGENT_SESSION_ID: sessionId,
            MONITORING_CHANNEL: process.env.MONITORING_CHANNEL || "cron",
          },
        },
      },
    },
  });

  for await (const message of q) {
    if (message.type === "assistant") {
      for (const block of message.message.content) {
        if (block.type === "text") process.stdout.write(block.text);
        else if (block.type === "tool_use") process.stdout.write(`\n  → ${block.name}(${JSON.stringify(block.input)})\n`);
      }
    } else if (message.type === "result") {
      const cost = (message as any).total_cost_usd;
      console.log(`\n\n=== run complete${cost != null ? ` ($${cost.toFixed(4)})` : ""} ===`);
    }
  }
}

const prompt =
  process.argv.slice(2).join(" ") ||
  "Run a monitoring pass: triage the open issues, diagnose the highest-impact one, and if it " +
    "warrants it, declare an incident (wait for the human gate) and state the outcome.";

run(prompt).catch((e) => {
  console.error(e);
  process.exit(1);
});
