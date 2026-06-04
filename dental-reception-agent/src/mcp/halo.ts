// ============================================================================
// Halo — the content-addressed store at the tool-result boundary.
//
// Heavy tool results are NOT returned to the model raw. Instead the heavy parts
// are written to agent.halo_nodes keyed by a content handle (h:sha256:...), and
// the tool returns a compact ENVELOPE: a small summary plus `refs` (handles) to
// the heavy parts. The model reasons on the envelope and fetches only the
// handles a step actually needs (halo_fetch / halo_fetch_many).
//
// Because the store is Postgres-backed and persistent, a handle seen early in a
// session is fetchable late. Maps (agent.halo_maps) keep the latest root per
// entity (e.g. a patient id) so repeated calls about the same patient fold into
// one growing map — "argument-join".
// ============================================================================
import { createHash } from "node:crypto";
import type { PoolClient } from "pg";

export type Handle = string; // "h:sha256:<hex>"

/** An envelope is what the model actually sees: a compact summary + handles. */
export interface Envelope {
  kind: string;
  summary: unknown;
  refs: Record<string, Handle>;
  map_root?: Handle;
}

function handleFor(bytes: Buffer): Handle {
  return "h:sha256:" + createHash("sha256").update(bytes).digest("hex");
}

/** Store a JSON value as a content-addressed node; return its handle. */
export async function putJson(client: PoolClient, value: unknown): Promise<Handle> {
  const bytes = Buffer.from(JSON.stringify(value), "utf8");
  const handle = handleFor(bytes);
  await client.query(
    "INSERT INTO agent.halo_nodes (handle, bytes) VALUES ($1, $2) ON CONFLICT (handle) DO NOTHING",
    [handle, bytes],
  );
  return handle;
}

/** Fetch a single node's decoded JSON. */
export async function getJson(client: PoolClient, handle: Handle): Promise<unknown> {
  const r = await client.query("SELECT bytes FROM agent.halo_nodes WHERE handle = $1", [handle]);
  if (r.rowCount === 0) return { error: "handle_not_found", handle };
  const bytes: Buffer = r.rows[0].bytes;
  return JSON.parse(bytes.toString("utf8"));
}

/** Fetch many nodes in one round trip (batched drill-down). */
export async function getMany(client: PoolClient, handles: Handle[]): Promise<Record<Handle, unknown>> {
  if (handles.length === 0) return {};
  const r = await client.query(
    "SELECT handle, bytes FROM agent.halo_nodes WHERE handle = ANY($1)",
    [handles],
  );
  const found = new Map<string, Buffer>(r.rows.map((row: any) => [row.handle, row.bytes]));
  const out: Record<Handle, unknown> = {};
  for (const h of handles) {
    const b = found.get(h);
    out[h] = b ? JSON.parse(b.toString("utf8")) : { error: "handle_not_found", handle: h };
  }
  return out;
}

/**
 * Encode a tool result into an envelope: stash each heavy field under a handle,
 * keep a compact summary inline. `heavy` maps a ref name to the value to stash.
 */
export async function encode(
  client: PoolClient,
  kind: string,
  summary: unknown,
  heavy: Record<string, unknown>,
): Promise<Envelope> {
  const refs: Record<string, Handle> = {};
  for (const [name, value] of Object.entries(heavy)) {
    if (value !== undefined) refs[name] = await putJson(client, value);
  }
  return { kind, summary, refs };
}

/**
 * Accumulate an envelope into the per-entity map (argument-join). Stores the
 * envelope as a node and records it as the latest root for (session, mapId).
 */
export async function accumulate(
  client: PoolClient,
  sessionId: string,
  mapId: string,
  envelope: Envelope,
  source: unknown,
): Promise<Handle> {
  const root = await putJson(client, envelope);
  await client.query(
    `INSERT INTO agent.halo_maps (session_id, map_id, root, source, updated_at)
     VALUES ($1, $2, $3, $4, now())
     ON CONFLICT (session_id, map_id) DO UPDATE SET root = EXCLUDED.root,
       source = EXCLUDED.source, updated_at = now()`,
    [sessionId, mapId, root, source ?? null],
  );
  return root;
}
