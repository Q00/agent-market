// 수사관이 승인한 판단을 로컬 파일에 앞 기록 해시로 연결해 저장합니다.
import { createHash, randomUUID } from "node:crypto";
import { appendFileSync, existsSync, mkdirSync, readFileSync } from "node:fs";
import { DATA_DIR } from "./paths.js";
import { anchorFinding } from "./chain-adapter.js";

const FILE = `${DATA_DIR}/findings.jsonl`;

function lastHash() {
  if (!existsSync(FILE)) return null;
  const lines = readFileSync(FILE, "utf8").trim().split("\n").filter(Boolean);
  return lines.length ? JSON.parse(lines.at(-1)).record_hash : null;
}

export async function recordFinding(store, input) {
  const found = input.evidence_seqs.filter((s) => store.bySeq.has(s));
  const missing = input.evidence_seqs.filter((s) => !store.bySeq.has(s));
  const body = {
    record_id: randomUUID(),
    recorded_at: new Date().toISOString(),
    case_id: input.case_id,
    shop: store.anon.name(store.resolveShop(input.shop), "shop"),
    finding: input.finding,
    evidence_seqs: input.evidence_seqs,
    decided_by: input.decided_by,
    approver: input.approver ?? null,
    recipe_id: input.recipe_id,
    recipe_author: input.recipe_author,
    prev_record_hash: lastHash(),
  };
  const record_hash = createHash("sha256").update(JSON.stringify(body)).digest("hex");
  const anchor = await anchorFinding({ ...body, record_hash });
  mkdirSync(DATA_DIR, { recursive: true });
  appendFileSync(FILE, `${JSON.stringify({ ...body, record_hash, anchor })}\n`, { mode: 0o600 });
  return {
    record_id: body.record_id,
    case_id: body.case_id,
    shop: body.shop,
    decided_by: body.decided_by,
    approver: body.approver,
    record_hash,
    evidence_check: { found_in_current_window: found, not_in_current_window: missing, data_source: store.sourceInfo() },
    ...anchor,
  };
}
