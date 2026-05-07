/**
 * Static-snapshot data layer.
 *
 * For the Vercel demo deployment we don't run the FastAPI backend.
 * Instead, `scripts/export_dashboard_snapshot.py` writes JSON files
 * into `public/snapshot/` mirroring each API path. Server components
 * read those files via fs at request time.
 *
 * Live mode is restored by setting `OPS_API_BASE` (server) or
 * unsetting `NEXT_PUBLIC_USE_SNAPSHOT`.
 */

import { promises as fs } from "node:fs";
import path from "node:path";

const SNAPSHOT_ROOT = path.join(
  process.cwd(),
  "public",
  "snapshot",
);

export async function readSnapshot<T>(file: string): Promise<T> {
  const full = path.join(SNAPSHOT_ROOT, file);
  const raw = await fs.readFile(full, "utf8");
  return JSON.parse(raw) as T;
}

export async function snapshotExists(): Promise<boolean> {
  try {
    await fs.access(path.join(SNAPSHOT_ROOT, "kpis.json"));
    return true;
  } catch {
    return false;
  }
}
