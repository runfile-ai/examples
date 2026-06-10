// ============================================================================
// Live Credit-Line Decision Agent on LangGraph.js.
//
// Loads the SAME `mimic-creditline` MCP tools used by the Claude and OpenAI
// builds (via @langchain/mcp-adapters' MultiServerMCPClient over stdio, spawning
// the server from the sibling base project), wires them into a LangGraph
// prebuilt ReAct agent, and runs one credit-line request end to end — including
// blocking on the human-in-the-loop approval gate.
//
//   npm run agent                 (uses the seeded escalation request)
//   npm run agent -- <REQUEST_ID>
//
// Requires a model API key for the configured provider (ANTHROPIC_API_KEY by
// default) and a seeded mimic_creditline database. Resolve any pending approval
// from a second terminal with the base project's `npm run officer` (the HITL
// surface is shared).
// ============================================================================
import { fileURLToPath } from "node:url";
import { dirname, join, resolve } from "node:path";
import { MultiServerMCPClient } from "@langchain/mcp-adapters";
import { createReactAgent } from "@langchain/langgraph/prebuilt";
import { initChatModel } from "langchain/chat_models/universal";
import { INSTRUCTIONS, MODEL, MODEL_PROVIDER } from "./prompts.js";

try { process.loadEnvFile(); } catch { /* env may come from the shell */ }

const __dirname = dirname(fileURLToPath(import.meta.url));
const PROJECT_ROOT = resolve(__dirname, "..", "..");
// The reusable environment (MCP server + DB) lives in the sibling base project.
const SIBLING = resolve(PROJECT_ROOT, "..", "creditline-decision-agent-ts");
const SERVER_PATH = join(SIBLING, "src", "mcp", "server.ts");
const TSX_BIN = join(PROJECT_ROOT, "node_modules", ".bin", "tsx");

const DEMO_REQUEST_ID = "11111111-1111-1111-1111-111111111111";

async function run(requestId: string) {
  const approvalTimeout = Number(process.env.APPROVAL_TIMEOUT_SECONDS || "900");

  const client = new MultiServerMCPClient({
    mcpServers: {
      "mimic-creditline": {
        transport: "stdio",
        command: TSX_BIN,
        args: [SERVER_PATH],
        cwd: SIBLING,
        env: {
          ...process.env,
          MIMIC_DB_DSN: process.env.MIMIC_DB_DSN ?? "",
          APPROVAL_TIMEOUT_SECONDS: String(approvalTimeout),
          APPROVAL_POLL_SECONDS: process.env.APPROVAL_POLL_SECONDS || "2",
        } as Record<string, string>,
        // The tool call to request_approval blocks on the human gate, so the
        // per-tool timeout must outlast it.
        defaultToolTimeout: (approvalTimeout + 30) * 1000,
      },
    },
  });

  const tools = await client.getTools();
  const model = await initChatModel(MODEL, { modelProvider: MODEL_PROVIDER, temperature: 0 });
  const agent = createReactAgent({ llm: model, tools, prompt: INSTRUCTIONS });

  const userPrompt =
    `Process credit-line request ${requestId} end to end. When you escalate, ` +
    `open the approval gate and wait for the credit officer's resolution, then ` +
    `state the final outcome.`;

  console.log(`=== Credit-Line Decision Agent (LangGraph) — request ${requestId} ===\n`);
  try {
    const result = await agent.invoke(
      { messages: [{ role: "user", content: userPrompt }] },
      { recursionLimit: 60 },
    );
    const messages = result.messages;
    console.log(String(messages[messages.length - 1].content));
    console.log("\n=== run complete ===");
  } finally {
    await client.close();
  }
}

const requestId = process.argv.slice(2).join(" ").trim() || DEMO_REQUEST_ID;
run(requestId).catch((e) => {
  console.error(e);
  process.exit(1);
});
