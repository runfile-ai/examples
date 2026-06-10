// Shared pg pool for the mimic-creditline MCP server. The DSN comes from
// MIMIC_DB_DSN and points at the least-privilege `creditline_agent` role.
//
// Type parsers: by default pg returns numeric/bigint as strings. We parse them
// to JS numbers so tool results are clean, domain-shaped JSON (mirroring the
// Python build, where asyncpg Decimals are jsonified to floats). jsonb and
// timestamptz are already decoded by pg (object / Date).
import pg from "pg";

try { process.loadEnvFile(); } catch { /* env may come from the parent process */ }

pg.types.setTypeParser(1700, (v) => (v === null ? null : Number(v))); // numeric
pg.types.setTypeParser(20, (v) => (v === null ? null : Number(v)));   // int8 / bigint

const dsn = process.env.MIMIC_DB_DSN;
if (!dsn) {
  throw new Error("MIMIC_DB_DSN is not set");
}

export const pool = new pg.Pool({ connectionString: dsn, max: 8 });

export async function withClient<T>(fn: (c: pg.PoolClient) => Promise<T>): Promise<T> {
  const client = await pool.connect();
  try {
    return await fn(client);
  } finally {
    client.release();
  }
}
