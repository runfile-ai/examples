// ============================================================================
// Live Credit-Line Decision Agent on the OpenAI Agents SDK (TypeScript).
//
// Connects to the SAME `mimic-creditline` MCP server used by the Claude and
// LangGraph builds (launched as a stdio subprocess from the sibling base
// project) and runs one credit-line request end to end, including blocking on
// the human-in-the-loop approval gate.
//
//   npm run agent                 (uses the seeded escalation request)
//   npm run agent -- <REQUEST_ID>
//
// Requires OPENAI_API_KEY and a seeded mimic_creditline database. Resolve any
// pending approval from a second terminal with the base project's
// `npm run officer` (the HITL surface is shared).
// ============================================================================
import { fileURLToPath } from "node:url";
import { dirname, join, resolve } from "node:path";
import { Agent, run, MCPServerStdio, setTracingDisabled } from "@openai/agents";
import { INSTRUCTIONS, MODEL_VERSION } from "./prompts.js";

try { process.loadEnvFile(); } catch { /* env may come from the shell */ }

const __dirname = dirname(fileURLToPath(import.meta.url));
const PROJECT_ROOT = resolve(__dirname, "..", "..");
// The reusable environment (MCP server + DB) lives in the sibling base project.
const SIBLING = resolve(PROJECT_ROOT, "..", "creditline-decision-agent-ts");
const SERVER_PATH = join(SIBLING, "src", "mcp", "server.ts");
const TSX_BIN = join(PROJECT_ROOT, "node_modules", ".bin", "tsx");

const DEMO_REQUEST_ID = "11111111-1111-1111-1111-111111111111";

async function run_(requestId: string) {
  // No Runfile capture here, so keep runs self-contained: disable OpenAI tracing
  // export.
  setTracingDisabled(true);

  const approvalTimeout = Number(process.env.APPROVAL_TIMEOUT_SECONDS || "900");

  const server = new MCPServerStdio({
    name: "mimic-creditline",
    command: TSX_BIN,
    args: [SERVER_PATH],
    cwd: SIBLING,
    env: {
      ...process.env,
      MIMIC_DB_DSN: process.env.MIMIC_DB_DSN ?? "",
      APPROVAL_TIMEOUT_SECONDS: String(approvalTimeout),
      APPROVAL_POLL_SECONDS: process.env.APPROVAL_POLL_SECONDS || "2",
    } as Record<string, string>,
    cacheToolsList: true,
    // The request_approval tool call blocks on the human gate; the client
    // session timeout must outlast it.
    clientSessionTimeoutSeconds: approvalTimeout + 30,
  });

  console.log(`=== Credit-Line Decision Agent (OpenAI Agents SDK) — request ${requestId} ===\n`);

  await server.connect();
  try {
    const agent = new Agent({
      name: "Credit-Line Decision Agent",
      instructions: INSTRUCTIONS,
      model: MODEL_VERSION,
      mcpServers: [server],
      modelSettings: { temperature: 0 },
    });

    const prompt =
      `Process credit-line request ${requestId} end to end. When you escalate, ` +
      `open the approval gate and wait for the credit officer's resolution, then ` +
      `state the final outcome.`;

    const result = await run(agent, prompt, { maxTurns: 40 });
    console.log(result.finalOutput);
  } finally {
    await server.close();
  }

  console.log("\n=== run complete ===");
}

const requestId = process.argv.slice(2).join(" ").trim() || DEMO_REQUEST_ID;
run_(requestId).catch((e) => {
  console.error(e);
  process.exit(1);
});
