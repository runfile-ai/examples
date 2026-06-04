// ============================================================================
// Live dental reception agent on the Claude Agent SDK (TypeScript).
//
// Registers the `dental` MCP server (stdio subprocess), loads the project
// Skills, and runs one reception session. Schedule/record writes block on the
// human approval gate inside the MCP tools — resolve them from another terminal
// with `npm run frontdesk`.
//
//   npm run agent            (uses the default session prompt)
//   npm run agent -- "..."   (custom caller request)
//
// Uses the Claude Code runtime, so it runs with the local `claude` login or an
// ANTHROPIC_API_KEY. A seeded `dental` database must exist.
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
].map((t) => `mcp__dental__${t}`);

async function run(userPrompt: string) {
  const sessionId = process.env.AGENT_SESSION_ID || randomUUID();
  console.log(`=== Dental reception agent (Claude Agent SDK) — session ${sessionId} ===\n`);

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
      // real safety gates are the identity check and the human approval queue
      // inside the MCP writes.
      canUseTool: async (_toolName, input) => ({ behavior: "allow", updatedInput: input }),
      mcpServers: {
        dental: {
          type: "stdio",
          command: TSX_BIN,
          args: [SERVER_PATH],
          env: {
            ...process.env,
            AGENT_SESSION_ID: sessionId,
            DENTAL_CHANNEL: process.env.DENTAL_CHANNEL || "voice",
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
      console.log(`\n\n=== session complete${cost != null ? ` ($${cost.toFixed(4)})` : ""} ===`);
    }
  }
}

const prompt =
  process.argv.slice(2).join(" ") ||
  "A caller says: \"Hi, this is Maria Garcia, 415-555-0142. I'd like to book a cleaning, " +
    "preferably a morning slot with Dr. Nguyen sometime next week.\" Verify her identity (DOB " +
    "1989-04-12), check her coverage for the cleaning, find a matching slot, hold it, and book it " +
    "(the booking waits for the front-desk confirmation). State the copay and the confirmed time.";

run(prompt).catch((e) => {
  console.error(e);
  process.exit(1);
});
