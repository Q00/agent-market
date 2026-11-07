import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
export const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..");
export const DATA_DIR = join(ROOT, "data");
export const SNAPSHOT_DIR = join(DATA_DIR, "snapshot");
export const FIXTURE_DIR = join(ROOT, "fixtures");
