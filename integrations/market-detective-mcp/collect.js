// 공개 API를 간격을 두고 읽어 data/snapshot에 저장합니다. 사용법: node collect.js [원장건수]
import { mkdirSync, renameSync, writeFileSync } from "node:fs";
import { SNAPSHOT_DIR } from "./lib/paths.js";
import { fetchRecentLedger, marketGet } from "./lib/market.js";
import { reviewFile } from "./lib/store.js";

const entries = Number(process.argv[2] || 10000);
const MIN_REVIEW_DEALS = 50;
mkdirSync(SNAPSHOT_DIR, { recursive: true });
const save = (name, value) => {
  const tmp = `${SNAPSHOT_DIR}/.${name}.tmp`;
  writeFileSync(tmp, JSON.stringify(value));
  renameSync(tmp, `${SNAPSHOT_DIR}/${name}`);
};

const ledger = await fetchRecentLedger(entries);
save("ledger.json", ledger);
console.log(`원장 ${ledger.length}건`);
const board = await marketGet("/api/board");
save("board.json", board);
const round = board.round ?? 4;
save("shops.json", await marketGet("/api/shops"));
try {
  save("chain.json", await marketGet("/api/chain"));
  console.log("체인 정보 저장");
} catch {
  console.log("체인 정보 없음(/api/chain 미제공)");
}
const forum = await marketGet("/api/forum?limit=500");
save("forum.json", forum);
console.log(`포럼 ${forum.length}건`);
const catalog = await marketGet("/api/catalog");
save("catalog.json", Array.isArray(catalog) ? catalog : catalog.items ?? catalog.products ?? []);
const targets = board.shops.filter((s) => s.deals >= MIN_REVIEW_DEALS);
for (const s of targets) {
  save(reviewFile(s.name), await marketGet(`/api/shop-reviews?shop=${encodeURIComponent(s.name)}&round=${round}`));
}
console.log(`리뷰 수집 상점 ${targets.length}곳`);
save("meta.json", {
  captured_at: new Date().toISOString(),
  ledger_entries: ledger.length,
  review_shops_min_deals: MIN_REVIEW_DEALS,
});
console.log("스냅샷 저장 완료");
