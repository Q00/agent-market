// 수사 상황판: 읽기 전용 로컬 화면 서버. MCP 서버와 별도 프로세스이며 비밀 토큰을 읽지 않습니다.
// 사용법: node board/server.js [--port 8790] [--snapshot] [--findings 경로]
import { existsSync, readFileSync, statSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import express from "express";
import { pseudonym } from "../lib/anon.js";
import { fetchRecentLedger, marketGet } from "../lib/market.js";
import { DATA_DIR, SNAPSHOT_DIR } from "../lib/paths.js";

const HERE = dirname(fileURLToPath(import.meta.url));
const arg = (flag) => {
  const i = process.argv.indexOf(flag);
  return i > -1 ? process.argv[i + 1] : undefined;
};
const PORT = Number(arg("--port") || process.env.BOARD_PORT || 8790);
const FINDINGS_FILE = arg("--findings") || `${DATA_DIR}/findings.jsonl`;
const WANT_SNAPSHOT = process.argv.includes("--snapshot");
const WINDOW = 3000;
const POLL_MS = 4000;
const META_MS = 30000;

const state = {
  mode: WANT_SNAPSHOT ? "snapshot" : "live",
  entries: [],
  lastSeq: 0,
  board: null,
  chain: null,
  urlToShop: new Map(),
  anonToReal: new Map(),
  error: null,
  replay: [],
};

const shopName = (urlOrName) => state.urlToShop.get(urlOrName) ?? urlOrName;
const anonShop = (urlOrName) => {
  if (!urlOrName) return null;
  const real = shopName(urlOrName);
  const name = pseudonym("shop", real);
  state.anonToReal.set(name, real);
  return name;
};
const anonBuyer = (name) => (name ? pseudonym("buyer", name) : null);

function setMeta(board, shops, chain) {
  if (board) state.board = board;
  if (chain) state.chain = chain;
  for (const s of shops?.shops ?? []) if (s.url) state.urlToShop.set(s.url, s.name);
  for (const s of board?.shops ?? []) if (s.card?.url) state.urlToShop.set(s.card.url, s.name);
}

function ingest(list) {
  for (const e of list) {
    if (e.kind === "join" && e.data?.role === "shop" && e.data.url) state.urlToShop.set(e.data.url, e.data.name);
    state.entries.push(e);
    state.lastSeq = Math.max(state.lastSeq, e.seq);
  }
  if (state.entries.length > WINDOW) state.entries.splice(0, state.entries.length - WINDOW);
}

const readSnapshot = (f) => JSON.parse(readFileSync(`${SNAPSHOT_DIR}/${f}`, "utf8"));

function startSnapshot() {
  state.mode = "snapshot";
  const ledger = readSnapshot("ledger.json");
  setMeta(readSnapshot("board.json"), readSnapshot("shops.json"), existsSync(`${SNAPSHOT_DIR}/chain.json`) ? readSnapshot("chain.json") : null);
  // 앞부분은 분석 창으로 채우고, 끝의 일부는 시간차로 흘려보내 화면이 계속 움직이게 합니다.
  const cut = Math.max(0, ledger.length - 120);
  ingest(ledger.slice(0, cut));
  state.replay = ledger.slice(cut);
  state.replayTimer = setInterval(() => {
    const next = state.replay.shift();
    if (next) ingest([next]);
  }, 1200);
}

async function refreshMeta() {
  const [board, shops] = await Promise.all([marketGet("/api/board"), marketGet("/api/shops")]);
  setMeta(board, shops);
}

// 시작할 때 시장에 닿지 않으면 스냅샷을 재생하다가, 연결되면 실시간으로 넘어갑니다.
async function connectLive() {
  const [board, shops] = await Promise.all([marketGet("/api/board"), marketGet("/api/shops")]);
  const chain = await marketGet("/api/chain").catch(() => null);
  const ledger = await fetchRecentLedger(2000);
  clearInterval(state.replayTimer);
  state.replay = [];
  state.entries = [];
  state.lastSeq = 0;
  state.mode = "live";
  state.error = null;
  setMeta(board, shops, chain);
  ingest(ledger);
}

async function startLive() {
  try {
    await connectLive();
  } catch (err) {
    console.error("시장 연결 실패, 스냅샷으로 전환:", err.message);
    state.error = "시장 연결 실패";
    startSnapshot();
    const retry = setInterval(async () => {
      try {
        await connectLive();
        clearInterval(retry);
        startPolling();
        console.log("시장 연결 복구, 실시간으로 전환");
      } catch {
        // 다음 주기에 다시 시도합니다.
      }
    }, 20000);
    return;
  }
  startPolling();
}

function startPolling() {
  setInterval(async () => {
    try {
      const page = await marketGet(`/api/ledger?after=${state.lastSeq}`);
      ingest(page);
      state.error = null;
    } catch (err) {
      state.error = "시장 응답 지연";
      console.error("원장 갱신 실패:", err.message);
    }
  }, POLL_MS);
  setInterval(() => refreshMeta().catch((err) => console.error("현황 갱신 실패:", err.message)), META_MS);
}

function maskText(text) {
  let out = String(text ?? "").replace(/https?:\/\/[^\s"'<>)]+/g, "[URL]");
  const names = new Map();
  for (const n of state.urlToShop.values()) if (n?.length >= 3) names.set(n, "shop");
  for (const b of state.board?.buyers ?? []) if (b.name?.length >= 3) names.set(b.name, "buyer");
  for (const [name, type] of [...names].sort((a, b) => b[0].length - a[0].length)) {
    out = out.split(name).join(pseudonym(type, name));
  }
  return out;
}

// 검증 항목 중 정산 외 항목의 실패는 영수증 자체의 문제로, 정산 실패와 구분합니다.
function resultStatus(d) {
  if (!d.receipt) return "walkaway";
  const failed = Object.entries(d.checks ?? {}).filter(([k, v]) => v === false && k !== "settled");
  if (failed.length) return "failed";
  return d.verified ? "verified" : "unsettled";
}

function feedItem(e) {
  const d = e.data ?? {};
  const base = { seq: e.seq, ts: e.ts, kind: e.kind, hash: e.hash?.slice(0, 10) ?? null };
  switch (e.kind) {
    case "result":
      return { ...base, buyer: anonBuyer(d.buyer), shop: anonShop(d.shop), amount: d.receipt?.price ?? null, stars: d.stars ?? null, status: resultStatus(d) };
    case "settle":
      return { ...base, buyer: anonBuyer(d.buyer), shop: anonShop(d.shop), amount: d.amount ?? null, honest: d.claim_honest ?? null };
    case "order":
      return { ...base, buyer: anonBuyer(d.buyer) };
    case "broken":
      return { ...base, buyer: anonBuyer(d.buyer), shop: anonShop(d.shop), amount: d.refunded ?? null };
    case "supply":
      return { ...base, shop: anonShop(d.shop), qty: d.qty ?? null };
    case "join":
      return { ...base, role: d.role, name: d.role === "shop" ? anonShop(d.name) : anonBuyer(d.name) };
    default:
      return base;
  }
}

let findingsCache = { mtime: 0, items: [] };
function readFindings() {
  if (!existsSync(FINDINGS_FILE)) return [];
  const mtime = statSync(FINDINGS_FILE).mtimeMs;
  if (mtime !== findingsCache.mtime) {
    const items = [];
    for (const line of readFileSync(FINDINGS_FILE, "utf8").split("\n")) {
      if (!line.trim()) continue;
      try {
        const r = JSON.parse(line);
        items.push({
          id: r.record_id,
          at: r.recorded_at,
          case_id: maskText(r.case_id),
          shop: r.shop,
          finding: maskText(r.finding).slice(0, 220),
          evidence_seqs: (r.evidence_seqs ?? []).slice(0, 12),
          decided_by: r.decided_by,
          approved: Boolean(r.approver),
          recipe_id: maskText(r.recipe_id),
          record_hash: r.record_hash?.slice(0, 16) ?? null,
          prev_hash: r.prev_record_hash?.slice(0, 16) ?? null,
          simulated: r.anchor?.simulated !== false,
          tx_hash: r.anchor?.tx_hash ?? null,
          payout: r.anchor?.payout ?? null,
        });
      } catch {
        console.error("사건 기록 한 줄을 읽지 못했습니다.");
      }
    }
    findingsCache = { mtime, items };
  }
  return findingsCache.items;
}

function shopStats() {
  const stats = new Map();
  for (const e of state.entries) {
    if (e.kind !== "result" && e.kind !== "settle") continue;
    const name = anonShop(e.data?.shop);
    if (!name) continue;
    const s = stats.get(name) ?? { shop: name, results: 0, failed: 0, settled: 0 };
    if (e.kind === "settle") s.settled += 1;
    else {
      s.results += 1;
      if (resultStatus(e.data) === "failed") s.failed += 1;
    }
    stats.set(name, s);
  }
  return [...stats.values()].sort((a, b) => b.failed - a.failed || b.results - a.results);
}

function caseFile(requested) {
  const findings = readFindings();
  const ranked = shopStats();
  const shop = requested || findings.at(-1)?.shop || ranked[0]?.shop || null;
  if (!shop) return { found: false, ranked: [] };
  const buyers = new Map();
  const reviews = new Map();
  const evidence = [];
  const dishonest = [];
  let results = 0;
  let starsSum = 0;
  let starsN = 0;
  let settled = 0;
  for (const e of state.entries) {
    if (anonShop(e.data?.shop) !== shop) continue;
    const d = e.data;
    if (e.kind === "settle") {
      settled += 1;
      if (d.claim_honest === false) {
        dishonest.push(e.seq);
        evidence.push(e.seq);
      }
    } else if (e.kind === "result") {
      results += 1;
      const status = resultStatus(d);
      const name = anonBuyer(d.buyer) ?? "미상";
      const b = buyers.get(name) ?? { name, results: 0, verified: 0, failed: 0, unsettled: 0 };
      b.results += 1;
      if (status === "verified") b.verified += 1;
      if (status === "failed") {
        b.failed += 1;
        evidence.push(e.seq);
      }
      if (status === "unsettled" || status === "walkaway") b.unsettled += 1;
      buyers.set(name, b);
      if (typeof d.stars === "number") {
        starsSum += d.stars;
        starsN += 1;
      }
      if (typeof d.review === "string" && d.review.length > 12) reviews.set(d.review, (reviews.get(d.review) ?? 0) + 1);
    }
  }
  const real = state.anonToReal.get(shop);
  const row = (state.board?.shops ?? []).find((s) => s.name === real);
  const top = [...buyers.values()].sort((a, b) => b.results - a.results);
  return {
    found: results + settled > 0 || Boolean(row),
    shop,
    window: { entries: state.entries.length, from: state.entries[0]?.seq ?? null, to: state.lastSeq },
    results,
    settled,
    stars: starsN ? Math.round((starsSum / starsN) * 100) / 100 : null,
    buyers_total: top.length,
    top_share: results && top[0] ? Math.round((top[0].results / results) * 100) : null,
    buyers: top.slice(0, 14),
    repeated_review: Math.max(0, ...reviews.values()),
    dishonest_seqs: dishonest.slice(0, 5),
    evidence_seqs: [...new Set(evidence)].slice(-5),
    board: row
      ? { house: row.house, deals: row.deals, stars: row.stars, reviews: row.reviews, claims: row.claims, honest_claims: row.honest_claims, refunds: row.refunds, broken: row.broken }
      : null,
    findings: findings.filter((f) => f.shop === shop).length,
    ranked: ranked.slice(0, 12).map((r) => r.shop),
  };
}

const app = express();
app.disable("x-powered-by");
app.get("/", (_req, res) => res.sendFile(join(HERE, "index.html")));
app.get("/api/state", (_req, res) => {
  const totals = state.board?.totals ?? {};
  res.json({
    mode: state.mode,
    error: state.error,
    round: state.board?.round ?? null,
    totals: { results: totals.results ?? null, agents_online: totals.agents_online ?? null, rejected: totals.rejected ?? null },
    chain: state.chain ? { network: state.chain.network, asset: state.chain.asset } : null,
    latest_seq: state.lastSeq,
    feed: state.entries.slice(-40).map(feedItem),
    findings: readFindings(),
  });
});
app.get("/api/case", (req, res) => res.json(caseFile(typeof req.query.shop === "string" ? req.query.shop.slice(0, 40) : null)));

(WANT_SNAPSHOT ? Promise.resolve(startSnapshot()) : startLive()).finally(() => {
  app.listen(PORT, "127.0.0.1", () => console.log(`수사 상황판: http://127.0.0.1:${PORT} (${state.mode})`));
});
