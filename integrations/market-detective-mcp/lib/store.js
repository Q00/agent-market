// 분석에 쓰는 데이터 묶음. 스냅샷(시연 안정성)과 라이브(공개 API)를 같은 형태로 제공합니다.
import { existsSync, readFileSync, statSync } from "node:fs";
import { createHash } from "node:crypto";
import { SNAPSHOT_DIR } from "./paths.js";
import { Anonymizer } from "./anon.js";
import { fetchRecentLedger, marketGet } from "./market.js";

const LIVE_ENTRIES = Number(process.env.LIVE_LEDGER_ENTRIES || 2000);
const LIVE_TTL_MS = 60_000;
const readJson = (f) => JSON.parse(readFileSync(`${SNAPSHOT_DIR}/${f}`, "utf8"));
export const reviewFile = (name) => `reviews-${createHash("sha256").update(name).digest("hex").slice(0, 16)}.json`;

class Store {
  constructor({ mode, capturedAt, ledger, board, shops, forum, catalog }) {
    Object.assign(this, { mode, capturedAt, ledger, board, shops, forum, catalog });
    this.bySeq = new Map(ledger.map((e) => [e.seq, e]));
    this.urlToShop = new Map();
    this.shopOwner = new Map();
    this.buyerOwner = new Map();
    this.ownerAgents = new Map();
    this.anon = new Anonymizer();
    const addOwner = (agent, role, login) => {
      if (!login) return;
      (role === "shop" ? this.shopOwner : this.buyerOwner).set(agent.toLowerCase(), login);
      const s = this.ownerAgents.get(login) ?? { shops: new Set(), buyers: new Set() };
      s[role === "shop" ? "shops" : "buyers"].add(agent);
      this.ownerAgents.set(login, s);
      this.anon.add("owner", login);
    };
    for (const s of shops?.shops ?? []) {
      this.urlToShop.set(s.url, s.name);
      this.anon.add("shop", s.name);
      this.anon.add("shop", s.url, s.name);
    }
    for (const s of board?.shops ?? []) {
      this.anon.add("shop", s.name);
      if (s.card?.url) {
        this.urlToShop.set(s.card.url, s.name);
        this.anon.add("shop", s.card.url, s.name);
      }
      if (s.card?.name) this.anon.add("shop", s.card.name, s.name);
      addOwner(s.name, "shop", s.owner?.login);
    }
    for (const b of board?.buyers ?? []) {
      this.anon.add("buyer", b.name);
      addOwner(b.name, "buyer", b.owner?.login);
    }
    for (const e of ledger) {
      if (e.kind === "join" && e.data?.role === "shop" && e.data.url) {
        this.urlToShop.set(e.data.url, e.data.name);
        this.anon.add("shop", e.data.name);
        this.anon.add("shop", e.data.url, e.data.name);
      } else if (e.kind === "join" && e.data?.name) {
        this.anon.add("buyer", e.data.name);
      } else if (e.kind === "result" && e.data?.receipt?.shop && e.data.shop) {
        const canon = this.urlToShop.get(e.data.shop) ?? e.data.shop;
        this.anon.add("shop", e.data.receipt.shop, canon);
      }
    }
    for (const e of ledger) {
      if (e.data?.shop && typeof e.data.shop === "string" && e.data.shop.startsWith("http") && !this.urlToShop.has(e.data.shop)) {
        this.anon.add("shop", e.data.shop);
      }
    }
    for (const p of forum ?? []) {
      this.anon.add(p.role === "shop" ? "shop" : "buyer", p.author);
      if (typeof p.owner === "string") this.anon.add("owner", p.owner);
    }
    this.anon.finalize();
    this.catalogBySku = new Map((catalog ?? []).map((c) => [c.sku, c]));
  }
  shopName(urlOrName) {
    return this.urlToShop.get(urlOrName) ?? urlOrName;
  }
  resolveShop(input) {
    const raw = this.anon.resolve(input);
    const hit = (this.board?.shops ?? []).find((s) => s.name.toLowerCase() === raw.toLowerCase());
    return hit?.name ?? this.shopName(raw);
  }
  sourceInfo() {
    const first = this.ledger[0];
    const last = this.ledger.at(-1);
    return {
      mode: this.mode,
      captured_at: this.capturedAt,
      ledger_entries: this.ledger.length,
      ledger_seq_range: first ? [first.seq, last.seq] : null,
      ledger_time_range_utc: first
        ? [new Date(first.ts * 1000).toISOString(), new Date(last.ts * 1000).toISOString()]
        : null,
      names_pseudonymized: true,
    };
  }
  async reviews(shopName) {
    if (this.mode === "snapshot") {
      const f = reviewFile(shopName);
      return existsSync(`${SNAPSHOT_DIR}/${f}`) ? { items: readJson(f), available: true } : { items: [], available: false };
    }
    const round = this.board?.round ?? 4;
    const items = await marketGet(`/api/shop-reviews?shop=${encodeURIComponent(shopName)}&round=${round}`);
    return { items, available: true };
  }
}

let snapshot = null;
let snapshotMtime = 0;
let live = null;
let liveAt = 0;

export const snapshotExists = () => existsSync(`${SNAPSHOT_DIR}/meta.json`);

function loadSnapshot() {
  const m = statSync(`${SNAPSHOT_DIR}/meta.json`).mtimeMs;
  if (!snapshot || m !== snapshotMtime) {
    const meta = readJson("meta.json");
    snapshot = new Store({
      mode: "snapshot",
      capturedAt: meta.captured_at,
      ledger: readJson("ledger.json"),
      board: readJson("board.json"),
      shops: readJson("shops.json"),
      forum: readJson("forum.json"),
      catalog: readJson("catalog.json"),
    });
    snapshotMtime = m;
  }
  return snapshot;
}

async function loadLive() {
  if (!live || Date.now() - liveAt > LIVE_TTL_MS) {
    const ledger = await fetchRecentLedger(LIVE_ENTRIES);
    const board = await marketGet("/api/board");
    const shops = await marketGet("/api/shops");
    const forum = await marketGet("/api/forum?limit=200");
    live = new Store({
      mode: "live",
      capturedAt: new Date().toISOString(),
      ledger,
      board,
      shops,
      forum,
      catalog: snapshotExists() ? readJson("catalog.json") : await marketGet("/api/catalog"),
    });
    liveAt = Date.now();
  }
  return live;
}

// source: snapshot | live. 기본값은 DATA_MODE 환경 변수이며, 없으면 스냅샷 우선입니다.
export async function getStore(source) {
  const want = source ?? process.env.DATA_MODE ?? (snapshotExists() ? "snapshot" : "live");
  if (want === "snapshot") {
    if (!snapshotExists()) throw new Error("스냅샷이 없습니다. node collect.js를 먼저 실행하세요.");
    return loadSnapshot();
  }
  return loadLive();
}
