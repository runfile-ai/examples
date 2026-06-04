// Shared pg pool for the MCP server. DSN points at the local `dental`
// database that holds both the ext.* and agent.* schemas.
import pg from "pg";

try { process.loadEnvFile(); } catch { /* env may come from the parent process */ }

const dsn = process.env.DENTAL_DB_DSN;
if (!dsn) {
  throw new Error("DENTAL_DB_DSN is not set");
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
