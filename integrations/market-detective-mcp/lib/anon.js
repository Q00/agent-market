// 상점·구매자·소유자 가명 처리. 실명 대응표는 data/name-map.json(.gitignore 대상)에만 저장합니다.
import { createHmac, randomBytes } from "node:crypto";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { DATA_DIR } from "./paths.js";

const SALT_FILE = `${DATA_DIR}/anon-salt`;
const MAP_FILE = `${DATA_DIR}/name-map.json`;
const LABEL = { shop: "상점", buyer: "구매자", owner: "소유자", agent: "참가자" };

function salt() {
  mkdirSync(DATA_DIR, { recursive: true });
  if (!existsSync(SALT_FILE)) writeFileSync(SALT_FILE, randomBytes(32).toString("hex"), { mode: 0o600 });
  return readFileSync(SALT_FILE, "utf8");
}
const SALT = salt();

export const pseudonym = (type, real) =>
  `${LABEL[type]}-${createHmac("sha256", SALT).update(`${type === "owner" ? "owner" : "agent"}:${real}`).digest("hex").slice(0, 6).toUpperCase()}`;

export class Anonymizer {
  constructor() {
    this.typed = new Map(); // "유형:실명/URL" → 가명
    this.forward = new Map(); // 본문 치환용 실명/URL → 가명 (같은 문자열이면 먼저 등록한 값 유지)
    this.reverse = new Map(); // 가명(대문자) → 실명
    this.replacers = [];
  }
  add(type, real, canonical = real) {
    if (!real || typeof real !== "string") return;
    const name = pseudonym(type, canonical);
    const key = real.toLowerCase();
    this.typed.set(`${type}:${key}`, name);
    if (!this.forward.has(key)) this.forward.set(key, name);
    this.reverse.set(name.toUpperCase(), canonical);
  }
  finalize() {
    const names = [...this.forward.keys()].filter((k) => k.length >= 4).sort((a, b) => b.length - a.length);
    const esc = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    // 이름이 많아 묶어서 한 번에 치환합니다.
    this.replacers = [];
    for (let i = 0; i < names.length; i += 200) {
      this.replacers.push(new RegExp(names.slice(i, i + 200).map(esc).join("|"), "gi"));
    }
    const map = Object.fromEntries([...this.reverse.entries()].sort());
    writeFileSync(MAP_FILE, JSON.stringify(map, null, 1), { mode: 0o600 });
  }
  name(real, type = "agent") {
    if (real == null) return null;
    return this.typed.get(`${type}:${String(real).toLowerCase()}`) ?? pseudonym(type, String(real));
  }
  text(s) {
    if (typeof s !== "string") return s;
    let out = s;
    for (const re of this.replacers) out = out.replace(re, (m) => this.forward.get(m.toLowerCase()) ?? m);
    return out.replace(/https?:\/\/[^\s"'<>)]+/g, "[URL]");
  }
  deep(v) {
    if (typeof v === "string") return this.text(v);
    if (Array.isArray(v)) return v.map((x) => this.deep(x));
    if (v && typeof v === "object") return Object.fromEntries(Object.entries(v).map(([k, x]) => [k, this.deep(x)]));
    return v;
  }
  // 입력은 가명, 실명, URL을 모두 받습니다.
  resolve(input) {
    const s = String(input).trim();
    return this.reverse.get(s.toUpperCase()) ?? s;
  }
}
