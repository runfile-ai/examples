// ============================================================================
// Live Credit-Line Decision Agent on the Claude Agent SDK (TypeScript).
//
// Registers the `mimic-creditline` MCP server as a stdio subprocess, loads the
// project Skills (intake / scoring / decisioning), and runs one credit-line
// request end to end — including blocking on the human-in-the-loop approval gate
// when the decision requires it.
//
//   npm run agent                 (uses the seeded escalation request)
//   npm run agent -- <REQUEST_ID>
//
// Uses the Claude Code runtime, so it runs with the local `claude` login or an
// ANTHROPIC_API_KEY. A seeded mimic_creditline database must exist. Resolve any
// pending approval from a second terminal with `npm run officer`.
// ============================================================================
import { fileURLToPath } from "node:url";
import { dirname, join, resolve } from "node:path";
import { query } from "@anthropic-ai/claude-agent-sdk";
import { SYSTEM_PROMPT, MODEL_VERSION } from "./prompts.js";

try { process.loadEnvFile(); } catch { /* env may come from the shell */ }

const __dirname = dirname(fileURLToPath(import.meta.url));
const PROJECT_ROOT = resolve(__dirname, "..", "..");
const SERVER_PATH = join(PROJECT_ROOT, "src", "mcp", "server.ts");
const TSX_BIN = join(PROJECT_ROOT, "node_modules", ".bin", "tsx");

const DEMO_REQUEST_ID = "11111111-1111-1111-1111-111111111111";

const MCP_TOOLS = [
  "creditline_get_agent_provenance",
  "creditline_get_request",
  "creditline_get_customer",
  "creditline_pull_bureau",
  "creditline_get_active_policy",
  "creditline_record_decision",
  "creditline_request_approval",
  "creditline_notify_customer",
].map((t) => `mcp__mimic-creditline__${t}`);

async function run(requestId: string) {
  const userPrompt =
    `Process credit-line request ${requestId}. Follow the full intake → bureau → ` +
    `policy → score → decide → record → human-approval flow. When you escalate, ` +
    `open the approval gate and wait for the credit officer's resolution, then ` +
    `state the final outcome.`;

  console.log(`=== Credit-Line Decision Agent (Claude Agent SDK) — request ${requestId} ===\n`);

  const q = query({
    prompt: userPrompt,
    options: {
      model: MODEL_VERSION,
      systemPrompt: SYSTEM_PROMPT,
      cwd: PROJECT_ROOT,
      settingSources: ["project"], // load .claude/skills + project settings
      allowedTools: [...MCP_TOOLS, "Skill"],
      // Approve tool use programmatically (no interactive prompt). The real
      // safety gate is the human approval queue inside the MCP writes.
      canUseTool: async (_toolName, input) => ({ behavior: "allow", updatedInput: input }),
      mcpServers: {
        "mimic-creditline": {
          type: "stdio",
          command: TSX_BIN,
          args: [SERVER_PATH],
          env: {
            ...process.env,
            MIMIC_DB_DSN: process.env.MIMIC_DB_DSN ?? "",
            APPROVAL_TIMEOUT_SECONDS: process.env.APPROVAL_TIMEOUT_SECONDS || "900",
            APPROVAL_POLL_SECONDS: process.env.APPROVAL_POLL_SECONDS || "2",
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

const requestId = process.argv.slice(2).join(" ").trim() || DEMO_REQUEST_ID;
run(requestId).catch((e) => {
  console.error(e);
  process.exit(1);
});
