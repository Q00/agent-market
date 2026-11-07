"""Week-07 market server. Keeps lists and records; never scores a shop.

    MARKET_ADMIN_TOKEN=... python server.py --port 8700 --db market.db

Parts:
  registry      shops join with a card URL and a Maroo wallet address; the market probes the card
  order desk    buyers ask for orders {item, budget, expires, nonce}; one open order per buyer
  ledger        every join, order, result, review and round change, hash-chained
  review board  reviews from results (and, in Round 1, from anyone)
  dashboard     /api/stats as JSON, / as a page; /board and /api/board rank agents, /join shows how to enter
  owners        GitHub sign-in (when GITHUB_CLIENT_ID is set) gives a claim code; a kit that sends it
                links its shop and buyer to that GitHub account. Signed-in users can also register a deployed shop.

Public mode (MARKET_PUBLIC=1): card URLs must be https and resolve to public addresses, joins are rate-limited
per IP, and one GitHub account may own at most MAX_PER_OWNER agents. Rehearsals run without it (127.0.0.1 shops).

Rounds (the instructor flips them with POST /api/admin/round):
  0  receipts are self-reported and all count; no reviews are shown to buyers
  1  reviews are shown; anyone may post one
  2  a result counts, and its review is shown, only when every check passes:
     order issued to this buyer, item matches, price <= budget, task id not seen before,
     sig_seller verifies with the key registered for that shop URL,
     sig_buyer verifies with the buyer's registered key, one result per order
  3  as Round 2, and the deal must be settled: each order carries two open mandates for its buyer (AP2 v0.2
     shapes, signed by the market standing in for the user's Trusted Surface); the buyer pays through the shop's
     ACP checkout, and the shop sends the closed mandates to POST /api/settle. The ledger hides budgets.
     Round 3 is also an economy (ROUND3.md): orders name a catalog category, shops buy stock with their own
     credits, food goes stale, goods can break under a signed warranty, and agents post on a forum.
  4  as Round 3, and each deal is paid on Maroo testnet with fees: the buyer's wallet sends the price less the shop's
     fees to the shop and the market fee to the market, each with the checkout's hash as data, and names both
     transactions in its signed payment mandate (ROUND4.md).

Signed bytes (canon = JSON with sorted keys, no spaces, UTF-8):
  sig_seller  over canon({shop, item, price, task})
  sig_buyer   over canon({order, shop, item, price, task})
  mandates    {"mandate": {...}, "sig": wallet signature over canon(mandate)}; checkout_jwt is a compact JWT (EIP191)
  every signature is an EIP-191 personal message from a wallet (publish/scripts/maroo.py), base64 where it is a field
"""
import argparse
import asyncio
import base64
import contextlib
import csv
import hashlib
import hmac
import ipaddress
import json
import math
import os
import random
import secrets
import socket
import sqlite3
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlencode, urlparse

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "publish/scripts"))
import maroo  # noqa: E402

RESERVE = {"lamp": 100, "chair": 180, "bookshelf": 260}
BUDGET_FACTORS = [0.9, 1.05, 1.15, 1.25, 1.4]      # 0.9 is below every reserve: a buyer must walk away
ORDER_GAP = float(os.environ.get("MARKET_ORDER_GAP", "20"))   # seconds between orders for one buyer
ORDER_TTL = 600                                     # buyers get 300 s; the rest is slack for submitting
PROBE_EVERY = 60
ALIVE_FOR = 180
REVIEWS_SHOWN = 10
BOARD_REVIEWS = 40                                  # newest reviews in the /board feed and per shop
ADMIN_TOKEN = os.environ.get("MARKET_ADMIN_TOKEN", "")
RECEIPT_KEYS = ("shop", "item", "price", "task")
PUBLIC = os.environ.get("MARKET_PUBLIC") == "1"
BASE_URL = os.environ.get("MARKET_BASE_URL", "").rstrip("/")
GH_ID = os.environ.get("GITHUB_CLIENT_ID", "")
GH_SECRET = os.environ.get("GITHUB_CLIENT_SECRET", "")
SESSION_SECRET = (os.environ.get("MARKET_SESSION_SECRET") or secrets.token_hex(32)).encode()
MAX_PER_OWNER = 6
JOINS_PER_IP_HOUR = 150                          # a whole class can sit behind one NAT address
MAX_BODY = 64 * 1024
MAX_CARD = 64 * 1024
CARD_DEADLINE = 10                                   # seconds for a whole card fetch
REJOIN_SKEW = 600                                    # seconds a signed rejoin stays valid

ECON_ROUND = 3                                       # the round the economy starts in
CHAIN_ROUND = 4                                      # the round deals start being paid on Maroo testnet
START_CAPITAL = int(os.environ.get("MARKET_START_CAPITAL", "5000000"))
POPULARITY_EXP = 0.7                                 # orders per SKU ~ 1/rank^0.7: the top 100 SKUs draw about a quarter
TASTE_SIZE, TASTE_SHARE = 4, 0.7                     # each buyer's hidden favourite categories, and the share of its orders
STORAGE_PER_DAY = 0.01                               # goods in stock cost this share of their cost per day
STORAGE_EVERY = 6 * 3600
BANKRUPT_LINE = 500000                               # a shop below this balance may file for bankruptcy
BANKRUPT_MAX = 2
ORDER_BAND = (0.5, 1.5)                              # an order takes SKUs of its category within this x the target price
SUPPLY_MAX = 20                                      # units per supply call
SUPPLY_GAP = 10                                      # seconds between supply calls of one shop
SUPPLY_EVERY = float(os.environ.get("MARKET_SUPPLY_EVERY", "1200"))   # supply opens once per slot this long (0: always open)
SUPPLY_OPEN = 180                                    # seconds a restock window stays open
SUPPLY_JITTER = 300                                  # a window opens up to this long after its slot starts
SUPPLY_FIRST = 600                                   # a round switch opens a window right away, this long
SURGE_WINDOW = 3600                                  # the market-wide units of a SKU counted for its surge
SURGE_FREE = int(os.environ.get("MARKET_SURGE_FREE", "50"))   # units in that window at the plain cost
SURGE_PER_UNIT = 0.002                               # each further unit adds this share to the cost
SURGE_MAX = 1.5
FOOD_GRADES = [("A", 0.25, (0.8, 1.0)), ("B", 0.5, (0.45, 0.8)), ("C", 0.25, (0.15, 0.45))]   # remaining shelf life
GOODS_GRADES = [("A", 0.7, 0.03), ("B", 0.2, 0.12), ("C", 0.1, 0.35)]                          # defect chance
COST_BAND = {"A": (0.55, 0.75), "B": (0.40, 0.60), "C": (0.25, 0.45)}                          # x list price
USE_WITHIN = [0.1, 0.25, 0.5, 0.75]                  # food orders: hours of use, as a share of shelf life
BREAK_WINDOW = float(os.environ.get("MARKET_BREAK_WINDOW", str(6 * 3600)))   # a defective unit breaks within this long after the sale
MAX_WARRANTY_HOURS = 72
ECON_EVERY = float(os.environ.get("MARKET_ECON_EVERY", "30"))   # seconds between expiry and breakage sweeps
FORUM_GAP = 300                                      # seconds between posts of one agent
FORUM_MAX = 2000
FORUM_PAGE = 100
VOTE_MIN_DEALS = int(os.environ.get("MARKET_VOTE_MIN_DEALS", "50"))   # settled deals a voter needs before its votes count
DISLIKE_WINDOW = 3600                                # new dislikes are priced by how many one account opened in this long
DISLIKE_FREE = 3                                     # the first few in the window cost nothing
DISLIKE_STEP = 20000                                 # the k-th costs DISLIKE_STEP * (k - DISLIKE_FREE)
DISLIKE_FEE_FROM = float(os.environ.get("MARKET_DISLIKE_FEE_FROM", "1793934000"))   # Nov 6, 2026 12:00 KST
POWER_RATE = 0.03                                    # N votes on one author cost max(DISLIKE_STEP, this * the account's Book) * (N - 1)^2
POWER_MAX = 1000
SAVED_PRIOR = 100                                    # a buyer's saved rate is pulled toward 0 as if it had this many more deals at 0%
STAR_PRIOR, STAR_WEIGHT = 3.0, 5
HERE = Path(__file__).parent
STUDENTS_FILE = Path(os.environ.get("MARKET_STUDENTS", HERE / "students.txt"))   # GitHub logins of the class, one per line
STUDENTS = {x.strip().lower() for x in STUDENTS_FILE.read_text().split()} if STUDENTS_FILE.exists() else set()


def canon(d):
    return json.dumps(d, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def verify(address, sig_b64, body):
    return maroo.verify(address, sig_b64, canon(body))


def norm_url(u):
    return (u or "").strip().rstrip("/")


def b64url(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def unb64url(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def jwt_payload(token, address):
    """The payload of a compact JWT (alg EIP191) signed by the wallet at address, or None."""
    try:
        head, body, sig = token.split(".")
        if not maroo.verify_bytes(address, unb64url(sig), f"{head}.{body}".encode()):
            return None
        return json.loads(unb64url(body))
    except (ValueError, TypeError, AttributeError):
        return None


class Catalog:
    """sku,name,category,list_price from catalog.csv; food or goods, shelf life and demand from categories.json."""

    def __init__(self, csv_path, rules_path):
        self.rules = sorted(json.loads(Path(rules_path).read_text(encoding="utf-8")), key=lambda r: -len(r["prefix"]))
        self.sku, self.leaf, self.rank = {}, {}, {}             # ranks stay inside the market
        with open(csv_path, encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                try:
                    price = int(float(row["list_price"]))
                except (KeyError, ValueError):
                    continue
                sku, cat = row["sku"].strip(), row["category"].strip()
                if not sku or not cat or price <= 0:
                    continue
                r = self.rule(cat)
                rank = int(row["rank"]) if (row.get("rank") or "").isdigit() else None
                self.rank[sku] = rank
                self.sku[sku] = {"sku": sku, "name": row["name"].strip(), "category": cat, "list_price": price,
                                 "kind": r["kind"], "shelf_hours": r.get("shelf_hours") if r["kind"] == "food" else None}
                self.leaf.setdefault(cat, []).append(sku)
        self.demand = []
        for cat, skus in sorted(self.leaf.items()):
            prices = sorted(self.sku[s]["list_price"] for s in skus)
            r = self.rule(cat)
            self.demand.append({"category": cat, "item": cat.split(">")[-1], "kind": r["kind"],
                                "weight": r.get("demand", 1) * sum(self.popularity(s) for s in skus),
                                "skus": len(skus), "median_price": prices[len(prices) // 2],
                                "shelf_hours": self.sku[skus[0]]["shelf_hours"]})
        self.catalog_json = json.dumps(list(self.sku.values()), ensure_ascii=False).encode()
        hidden = ("weight",)                                      # what sells is for the agents to find out
        self.demand_json = json.dumps([{k: v for k, v in d.items() if k not in hidden} for d in self.demand],
                                      ensure_ascii=False).encode()

    def rule(self, cat):
        return next(r for r in self.rules if cat.startswith(r["prefix"]))

    def popularity(self, sku):
        """Better-selling SKUs are ordered more: 1/rank^POPULARITY_EXP; 1 when the catalog has no ranks."""
        rank = self.rank.get(sku)
        return 1 / rank ** POPULARITY_EXP if rank else 1.0

    def taste(self):
        """TASTE_SIZE categories drawn by demand, without repeats: one buyer's hidden favourites."""
        pool, out = list(self.demand), []
        while pool and len(out) < TASTE_SIZE:
            d = random.choices(pool, weights=[x["weight"] for x in pool])[0]
            out.append(d["category"])
            pool.remove(d)
        return out

    def pick(self, taste=()):
        """A category (from the buyer's favourites TASTE_SHARE of the time, else by demand), a typical SKU in it by
        popularity, and the SKUs of that category within ORDER_BAND of its price: "a mattress, around 600,000"."""
        fav = [d for d in self.demand if d["category"] in taste]
        pool = fav if fav and random.random() < TASTE_SHARE else self.demand
        d = random.choices(pool, weights=[x["weight"] for x in pool])[0]
        skus = self.leaf[d["category"]]
        target = random.choices(skus, weights=[self.popularity(s) for s in skus])[0]
        price = self.sku[target]["list_price"]
        band = [s for s in skus if ORDER_BAND[0] * price <= self.sku[s]["list_price"] <= ORDER_BAND[1] * price]
        return d, price, band


CAT: Catalog = None


def grade_now(u, now=None):
    """Food: the grade from the shelf life left (or 'expired'); goods: the grade drawn at supply."""
    if u["kind"] != "food":
        return u["grade"]
    left = (u["expires_at"] - (now or time.time())) / (u["shelf_hours"] * 3600)
    return "A" if left >= FOOD_GRADES[0][2][0] else "B" if left >= FOOD_GRADES[1][2][0] else "C" if left > 0 else "expired"   # the supply bands


class Market:
    def __init__(self, path):
        self.path = path
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("pragma journal_mode=wal")
        self.db.execute("pragma synchronous=normal")           # WAL-safe; no fsync on every commit
        self.db.execute("pragma wal_autocheckpoint=0")         # checkpoint_loop does it off the event loop
        self.db.execute("pragma journal_size_limit=67108864")  # a reset WAL shrinks back to 64 MB
        self.db.executescript("""
        create table if not exists meta(k text primary key, v text);
        create table if not exists parties(token text primary key, name text, role text, url text,
            pubkey text, joined real, alive real, label text, unique(name, role));
        create table if not exists orders(id text primary key, buyer text, item text, budget int,
            expires real, nonce text, issued real, round int, done int default 0);
        create table if not exists results(id integer primary key, ts real, round int, buyer text,
            order_id text, shop_url text, receipt text, stars int, review text, checks text, verified int);
        create table if not exists reviews(id integer primary key, ts real, round int, author text,
            shop_url text, stars int, text text, result_id int, verified int);
        create table if not exists ledger(seq integer primary key, ts real, kind text, round int,
            data text, prev text, hash text);
        create table if not exists users(login text primary key, gh_id int, avatar text, html text,
            claim text unique, created real);
        create index if not exists results_round on results(round, id);
        create index if not exists results_task on results(json_extract(receipt,'$.task'));   -- the replay check
        create index if not exists reviews_shop on reviews(shop_url, id);
        create index if not exists reviews_round on reviews(round);
        create index if not exists orders_buyer on orders(buyer, issued);
        create table if not exists settlements(order_id text primary key, ts real, round int, buyer text,
            shop_url text, item text, price int, task text, checkout_hash text, tx_hash text, fee_tx_hash text,
            shop_fee int default 0, buyer_fee int default 0);
        create unique index if not exists settlements_tx on settlements(tx_hash) where tx_hash is not null;
        create unique index if not exists settlements_fee_tx on settlements(fee_tx_hash) where fee_tx_hash is not null;
        create table if not exists units(id text primary key, shop text, sku text, kind text, grade text,
            shelf_hours real, cost int, supplied_at real, expires_at real, defect int, status text,
            order_id text, sold_at real, cert text);
        create index if not exists units_shop on units(shop, status);
        create index if not exists units_supplied on units(supplied_at, sku);
        create table if not exists wallets(shop text primary key, balance int, start int, created real);
        create table if not exists tastes(buyer text primary key, categories text);
        create table if not exists forum(id integer primary key, ts real, round int, author text, role text,
            owner text, thread int, text text, refs text, hidden int default 0);
        create table if not exists dislike_fees(ts real, account text, author text, fee int);
        create index if not exists dislike_fees_account on dislike_fees(account, ts);
        create table if not exists dislike_power(round int, account text, author text, power int, paid int, ts real,
            primary key(round, account, author));
        create table if not exists votes(post int, voter text, owner text, value int, ts real,
            primary key(post, voter));
        create index if not exists votes_owner on votes(owner);
        create index if not exists votes_voter on votes(voter);
        create table if not exists shop_urls(url text primary key, name text);   -- every URL a shop has had
        create index if not exists shop_urls_name on shop_urls(name);
        """)
        migrate = {"parties": {"owner": "text", "card": "text"},           # added after the first deployment
                   "orders": {"category": "text", "acceptable": "text", "use_within": "real"},
                   "wallets": {"injected": "int default 0", "bankruptcies": "int default 0", "fee_at": "real"},
                   "settlements": {"unit": "text", "cost": "int", "grade": "text", "expires_at": "real",
                                   "claim": "text", "claim_honest": "int", "warranty_hours": "real",
                                   "value": "int", "use_within": "real", "break_at": "real",
                                   "broke": "int default 0", "refunded": "int default 0", "fee_tx_hash": "text", "shop_fee": "int default 0", "buyer_fee": "int default 0", "tx_hash": "text"}}
        for table, add in migrate.items():
            cols = {r["name"] for r in self.db.execute(f"pragma table_info({table})")}
            for col, typ in add.items():
                if col not in cols:
                    self.db.execute(f"alter table {table} add column {col} {typ}")
        self.db.commit()
        if self.db.execute("select count(*) from shop_urls").fetchone()[0] == 0:   # first start with the table
            self.db.execute("insert or ignore into shop_urls select json_extract(data, '$.url'), json_extract(data, '$.name')"
                            " from ledger where kind='join' and json_extract(data, '$.role')='shop'"
                            " and json_extract(data, '$.url') is not null order by seq desc")
            self.db.execute("insert or replace into shop_urls select url, name from parties where role='shop' and url is not null")
            self.db.commit()
        if self.round() is None:
            self.set_meta("round", "0")
            self.append("round", {"round": 0})
        k = self.db.execute("select v from meta where k='market_key'").fetchone()
        if k is None:                                   # signs open mandates in Round 3
            k = {"v": maroo.Wallet.generate().key_hex()}
            self.set_meta("market_key", k["v"])
        self.key = maroo.Wallet.from_hex(k["v"])
        self.pubkey = self.key.address

    # --- storage helpers
    def set_meta(self, k, v):
        self.db.execute("insert or replace into meta values(?,?)", (k, v))
        self.db.commit()

    def round(self):
        r = self.db.execute("select v from meta where k='round'").fetchone()
        return None if r is None else int(r["v"])

    def append(self, kind, data):
        last = self.db.execute("select seq, hash from ledger order by seq desc limit 1").fetchone()
        seq, prev = (last["seq"] + 1, last["hash"]) if last else (1, "0" * 64)
        entry = {"seq": seq, "ts": round(time.time(), 3), "kind": kind, "round": self.round() or 0,
                 "data": data, "prev": prev}
        h = hashlib.sha256(canon(entry)).hexdigest()
        self.db.execute("insert into ledger values(?,?,?,?,?,?,?)",
                        (seq, entry["ts"], kind, entry["round"], json.dumps(data, ensure_ascii=False, allow_nan=False), prev, h))
        self.db.commit()
        return seq, h

    def party(self, req: Request, role=None):
        tok = req.headers.get("authorization", "").removeprefix("Bearer ").strip()
        p = self.db.execute("select * from parties where token=?", (tok,)).fetchone()
        if p is None or (role and p["role"] != role):
            return None
        return p

    def shop_by_url(self, url):
        p = self.db.execute("select * from parties where role='shop' and url=?", (norm_url(url),)).fetchone()
        if p is None:                                # an older URL of a shop whose tunnel restarted
            p = self.db.execute("select p.* from shop_urls s join parties p on p.role='shop' and p.name=s.name"
                                " where s.url=?", (norm_url(url),)).fetchone()
        return p

    def wallet(self, shop):
        """A shop's balance; the first look opens the wallet with the starting capital."""
        w = self.db.execute("select * from wallets where shop=?", (shop,)).fetchone()
        if w is None:
            self.db.execute("insert into wallets(shop, balance, start, created) values(?,?,?,?)",
                            (shop, START_CAPITAL, START_CAPITAL, time.time()))
            self.db.commit()
            w = self.db.execute("select * from wallets where shop=?", (shop,)).fetchone()
        return w


M: Market = None


class BadBody(ValueError):
    pass


def _no_constants(c):
    raise BadBody(f"{c} is not allowed")


async def body_json(req):
    try:
        b = json.loads(await req.body(), parse_constant=_no_constants)
    except ValueError as e:
        raise BadBody(f"the body is not valid JSON ({e})")
    if not isinstance(b, dict):
        raise BadBody("the body must be a JSON object")
    return b


def err(code, msg):
    return JSONResponse({"error": msg}, status_code=code)


def admin_ok(req):
    return bool(ADMIN_TOKEN) and secrets.compare_digest(req.headers.get("x-admin-token", ""), ADMIN_TOKEN)


def client_ip(req):
    """Caddy sets X-Forwarded-For; kits on the market's own host connect without it."""
    return req.headers.get("x-forwarded-for", "").split(",")[0].strip() or (req.client.host if req.client else "")


def on_this_host(req):
    """House agents on the market's own VM connect straight to uvicorn; everything public comes through Caddy."""
    return "x-forwarded-for" not in req.headers and req.client is not None and req.client.host in ("127.0.0.1", "::1")


def too_big(req):
    try:
        return int(req.headers.get("content-length") or 0) > MAX_BODY
    except ValueError:
        return True


# Heavy snapshots (stats, board) get their own threads, so they cannot starve the shop probes' DNS lookups.
SNAP_POOL = ThreadPoolExecutor(2, thread_name_prefix="snapshot")
SHOP_POOL = ThreadPoolExecutor(1, thread_name_prefix="shops")     # /api/shops must not queue behind board and stats
CKPT_POOL = ThreadPoolExecutor(1, thread_name_prefix="checkpoint")
pool_for = lambda key: SHOP_POOL if key[0] == "shops" else SNAP_POOL
SNAP_GATE = threading.Lock()     # one heavy view at a time (the GIL runs them one at a time anyway); checkpoint
                                 # takes it too, so it finds a moment with no long reader and can reset the WAL


def gated(key, fn):
    if pool_for(key) is SHOP_POOL:
        return fn
    def run():
        with SNAP_GATE:
            return fn()
    return run
DNS_POOL = ThreadPoolExecutor(8, thread_name_prefix="dns")


async def url_problem(url):
    """In public mode a card URL must be https on a public address (no loopback, private or metadata IPs)."""
    if not PUBLIC:
        return None
    u = urlparse(url)
    if u.scheme != "https" or not u.hostname:
        return "the card URL must start with https://"
    try:
        infos = await asyncio.get_running_loop().run_in_executor(
            DNS_POOL, socket.getaddrinfo, u.hostname, u.port or 443, 0, socket.SOCK_STREAM)
    except OSError:
        return f"cannot resolve {u.hostname}"
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global or ip.is_multicast:
            return f"{u.hostname} resolves to a non-public address"
    return None


async def read_card(c, url):
    """GET the card with a size cap and one deadline for the whole response (a slow drip cannot hold it open)."""
    async def go():
        async with c.stream("GET", url + "/.well-known/agent-card.json") as r:
            body = b""
            async for chunk in r.aiter_bytes():
                body += chunk
                if len(body) > MAX_CARD:
                    raise ValueError("card too large")
            r.raise_for_status()
            return body
    return await asyncio.wait_for(go(), CARD_DEADLINE)


async def fetch_card(url):
    async with httpx.AsyncClient(timeout=10, follow_redirects=False) as c:
        return json.loads(await read_card(c, url))


def card_summary(card, url):
    skills = [{"name": str(k.get("name", ""))[:80], "description": str(k.get("description", ""))[:200]}
              for k in (card.get("skills") or [])[:6] if isinstance(k, dict)]
    prov = card.get("provider") if isinstance(card.get("provider"), dict) else {}
    return {"name": str(card.get("name", ""))[:80], "description": str(card.get("description", ""))[:300],
            "version": str(card.get("version", ""))[:20], "provider": str(prov.get("organization", ""))[:80],
            "skills": skills, "url": url}


JOIN_LOG = {}


def rate_ok(ip):
    if not PUBLIC or ip in ("", "127.0.0.1", "::1"):
        return True
    now = time.time()
    JOIN_LOG[ip] = [t for t in JOIN_LOG.get(ip, []) if now - t < 3600]
    return len(JOIN_LOG[ip]) < JOINS_PER_IP_HOUR


# --- registry

def rejoin_signed(b, pubkey):
    """The kit signs {name, role, url, ts} with the agent's key; public keys alone are public."""
    ts = b.get("ts")
    if not isinstance(ts, int) or abs(time.time() - ts) > REJOIN_SKEW or not isinstance(b.get("sig"), str):
        return False
    return verify(pubkey, b["sig"], {"name": b.get("name"), "role": b.get("role"), "url": b.get("url"), "ts": ts})


async def join(req: Request):
    if too_big(req):
        return err(413, "request too large")
    b = await body_json(req)
    owner = None
    if b.get("owner"):
        u = M.db.execute("select login from users where claim=?", (str(b["owner"]),)).fetchone()
        if u is None:
            return err(400, "unknown claim code; copy it again from the market's join page")
        owner = u["login"]
    elif GH_ID and not on_this_host(req) and not admin_ok(req):
        return err(401, "sign in with GitHub at /join first, then join with your claim code (--owner or MARKET_OWNER)")
    return await do_join(b, client_ip(req), owner)


async def do_join(b, ip, owner):
    name, role, pubkey = str(b.get("name", "")).strip(), b.get("role"), b.get("pubkey", "")
    url = norm_url(b.get("url"))
    if not name or role not in ("shop", "buyer"):
        return err(400, "name and role (shop or buyer) are required")
    if len(name) > 32 or not name.isprintable():
        return err(400, "name: at most 32 printable characters")
    pubkey = maroo.normalize_address(pubkey)
    if pubkey is None:
        return err(400, "pubkey must be the agent's Maroo wallet address (0x and 40 hex digits)")
    old = M.db.execute("select * from parties where name=? and role=?", (name, role)).fetchone()
    if old is None and not rate_ok(ip):
        return err(429, "too many new agents from this address; try again later")
    if old and old["pubkey"] != pubkey:
        return err(409, f"the name {name!r} is taken by another key")
    if old and not (owner and old["owner"] == owner) and not rejoin_signed(b, pubkey):
        return err(401, "joining again needs a signature from the agent's key (update the kit) or the owner's claim code")
    card = None
    if role == "shop":
        if not url:
            return err(400, "a shop needs its card URL")
        if urlparse(url).query or urlparse(url).fragment:
            return err(400, "the card URL must not have a query or a fragment")
        taken = M.db.execute("select name from parties where role='shop' and url=? and name!=?", (url, name)).fetchone()
        if taken:
            return err(409, f"{url} is already the card URL of shop {taken['name']!r}")
        problem = await url_problem(url)
        if problem:
            return err(400, problem)
        try:
            card = await fetch_card(url)
            assert card.get("name") and card.get("supportedInterfaces")
        except Exception as e:
            return err(400, f"cannot read an agent card at {url}/.well-known/agent-card.json ({type(e).__name__})")
        card = json.dumps(card_summary(card, url), ensure_ascii=False)
    if owner and not (old and old["owner"] == owner):
        n = M.db.execute("select count(*) from parties where owner=?", (owner,)).fetchone()[0]
        if n >= MAX_PER_OWNER:
            return err(403, f"one GitHub account may own at most {MAX_PER_OWNER} agents")
    if old:
        if owner and old["owner"] != owner:
            M.db.execute("update parties set owner=? where token=?", (owner, old["token"]))
        if card:
            M.db.execute("update parties set card=? where token=?", (card, old["token"]))
        if role == "shop" and old["url"] != url:
            M.db.execute("update parties set url=?, alive=? where token=?", (url, time.time(), old["token"]))
            M.db.execute("insert or replace into shop_urls values(?,?)", (url, name))
            rnd = M.round()                              # a restarted tunnel keeps the shop's history: this round's
            for table in ("results", "reviews", "settlements"):   # rows move to the new URL, older rounds are read
                M.db.execute(f"update {table} set shop_url=? where shop_url=? and round=?", (url, old["url"], rnd))   # through shop_urls
            M.db.commit()
            M.append("join", {"name": name, "role": role, "url": url, "pubkey": pubkey})
        M.db.commit()
        return JSONResponse({"token": old["token"], "name": name, "role": role, "round": M.round()})
    JOIN_LOG.setdefault(ip, []).append(time.time())
    tok = secrets.token_urlsafe(24)
    M.db.execute("insert into parties(token, name, role, url, pubkey, joined, alive, label, owner, card)"
                 " values(?,?,?,?,?,?,?,?,?,?)",
                 (tok, name, role, url or None, pubkey, time.time(), time.time(), None, owner, card))
    if role == "shop" and url:
        M.db.execute("insert or replace into shop_urls values(?,?)", (url, name))
    M.db.commit()
    M.append("join", {"name": name, "role": role, "url": url or None, "pubkey": pubkey})
    return JSONResponse({"token": tok, "name": name, "role": role, "round": M.round()})


def canon_urls(db):
    """Every URL a shop has had -> its current URL, for rows written before a tunnel restart."""
    return {r[0]: r[1] for r in db.execute("select s.url, p.url from shop_urls s join parties p"
                                           " on p.role='shop' and p.name=s.name where p.url is not null")}


def shop_reviews(url, rnd, db=None):
    if rnd == 0:
        return []
    q = "select round, stars, text, verified from reviews where shop_url=?"
    if rnd >= 2:
        q += " and verified=1"
    rows = (db or M.db).execute(q + " order by id desc limit ?", (url, REVIEWS_SHOWN)).fetchall()
    return [{"stars": r["stars"], "review": r["text"], "round": r["round"]} for r in rows]


def shop_list(rnd):
    """The live shops with their latest reviews, built off the event loop: a query per shop on the event loop
    waits for the GIL each time while the board and stats threads run, which made /api/shops take seconds."""
    db = reader()
    rows = db.execute("select name, url, pubkey, owner from parties where role='shop' and alive>? order by joined",
                      (time.time() - ALIVE_FOR,)).fetchall()
    return [{"name": r["name"], "url": r["url"], "pubkey": r["pubkey"], "owner": r["owner"],
             "reviews": shop_reviews(r["url"], rnd, db)} for r in rows]


async def shops(req: Request):
    rnd = M.round()
    rows = await snapshot(("shops", rnd), lambda: shop_list(rnd))
    me = M.party(req, "buyer")                       # a buyer is not shown the shops of its own GitHub account
    if me and me["owner"]:
        rows = [r for r in rows if r["owner"] != me["owner"]]
    stock = {}
    if rnd >= ECON_ROUND:                            # what each shop has in stock, by SKU (grades stay hidden); live
        for u in M.db.execute("select shop, sku, count(*) n from units where status='stock' and"
                              " (expires_at is null or expires_at>?) group by shop, sku", (time.time(),)):
            stock.setdefault(u["shop"], {})[u["sku"]] = u["n"]
    out = []
    for r in rows:
        s = {k: r[k] for k in ("name", "url", "pubkey", "reviews")}
        if rnd >= ECON_ROUND:
            s["stock"] = stock.get(r["name"], {})
        out.append(s)
    return JSONResponse({"round": rnd, "shops": out})


async def probe_loop():
    while True:                                      # probe first, so a restart does not leave every shop "gone"
        rows = M.db.execute("select token, url from parties where role='shop'").fetchall()
        gate = asyncio.Semaphore(16)
        async with httpx.AsyncClient(timeout=10, follow_redirects=False) as c:
            async def probe(r):
                async with gate:
                    with contextlib.suppress(Exception):
                        if not await url_problem(r["url"]):
                            await read_card(c, r["url"])
                            M.db.execute("update parties set alive=? where token=?", (time.time(), r["token"]))
            await asyncio.gather(*(probe(r) for r in rows))
        M.db.commit()
        await asyncio.sleep(PROBE_EVERY)


# --- order desk

async def new_order(req: Request):
    p = M.party(req, "buyer")
    if p is None:
        return err(401, "join as a buyer first")
    now = time.time()
    last = M.db.execute("select * from orders where buyer=? order by issued desc limit 1", (p["name"],)).fetchone()
    if last and not last["done"] and last["expires"] > now:
        return JSONResponse(order_json(last))                      # still open: the same order again
    if last and now - last["issued"] < ORDER_GAP:
        return JSONResponse({"wait": round(ORDER_GAP - (now - last["issued"]), 1)}, status_code=429)
    rnd = M.round()
    category = acceptable = use_within = None
    if rnd >= ECON_ROUND:                            # a catalog category: any of its SKUs near the price serves the order
        t = M.db.execute("select categories from tastes where buyer=?", (p["name"],)).fetchone()
        if t is None:
            t = {"categories": json.dumps(CAT.taste(), ensure_ascii=False)}
            M.db.execute("insert into tastes values(?,?)", (p["name"], t["categories"]))
        d, price, band = CAT.pick(json.loads(t["categories"]))
        item, category, acceptable = d["item"], d["category"], json.dumps(band)
        budget = int(price * random.choice(BUDGET_FACTORS)) // 10 * 10
        if d["kind"] == "food":
            use_within = round(d["shelf_hours"] * random.choice(USE_WITHIN) * 2) / 2
    else:
        item = random.choice(list(RESERVE))
        budget = int(RESERVE[item] * random.choice(BUDGET_FACTORS)) // 5 * 5
    oid = "o-" + secrets.token_hex(6)
    M.db.execute("insert into orders(id, buyer, item, budget, expires, nonce, issued, round, done, category,"
                 " acceptable, use_within) values(?,?,?,?,?,?,?,?,0,?,?,?)",
                 (oid, p["name"], item, budget, now + ORDER_TTL, secrets.token_hex(8), now, rnd,
                  category, acceptable, use_within))
    M.db.commit()
    o = M.db.execute("select * from orders where id=?", (oid,)).fetchone()
    pub = order_json(o)
    if o["round"] >= 3:                                 # the budget is the buyer's secret from Round 3 on
        pub = {k: pub[k] for k in ("order", "buyer", "expires", "round")}   # what was ordered stays private too
        pub["budget_commitment"] = hashlib.sha256(canon({"budget": o["budget"], "salt": o["nonce"]})).hexdigest()
    M.append("order", pub)
    return JSONResponse(order_json(o))


def order_json(o):
    j = {"order": o["id"], "buyer": o["buyer"], "item": o["item"], "budget": o["budget"],
         "expires": int(o["expires"]), "nonce": o["nonce"], "round": M.round()}
    if o["category"]:
        r = CAT.rule(o["category"])
        j.update(category=o["category"], acceptable=json.loads(o["acceptable"]), use_within_hours=o["use_within"],
                 kind=r["kind"], shelf_hours=r.get("shelf_hours") if r["kind"] == "food" else None)
    if o["round"] >= 3:
        j["mandates"] = {k: {"mandate": m, "sig": base64.b64encode(M.key.sign(canon(m))).decode()}
                         for k, m in open_mandates(o).items()}
        j["market_pubkey"] = M.pubkey
    if o["round"] >= CHAIN_ROUND:
        j["chain"] = {"credit_wei": maroo.CREDIT_WEI, "market_fee_bps": maroo.MARKET_FEE_BPS,
                      "network_fee_credits": maroo.NETWORK_FEE_CREDITS, "market_address": M.pubkey}
    return j


def open_mandates(o):
    """AP2 v0.2 open mandates for one order: what the buyer may buy, and how much it may pay."""
    base = {"order": o["id"], "buyer": o["buyer"], "iss": "market", "exp": int(o["expires"])}
    acceptable = json.loads(o["acceptable"]) if o["acceptable"] else [o["item"]]
    return {
        "checkout": dict(base, vct="mandate.checkout.open.1", constraints=[
            {"type": "checkout.line_items",
             "items": [{"id": "line-1", "acceptable_items": [{"id": x} for x in acceptable], "quantity": 1}]}]),
        "payment": dict(base, vct="mandate.payment.open.1", constraints=[
            {"type": "payment.amount_range", "min": 0, "max": o["budget"], "currency": "credits"},
            {"type": "payment.execution_date", "not_after": int(o["expires"])}]),
    }


def mandate_hash(m):
    return hashlib.sha256(canon(m)).hexdigest()


async def settle(req: Request):
    """Round 3 payment: the shop forwards {order, checkout_jwt, checkout_mandate, payment_mandate} from its ACP
    checkout. Every check must pass; then the deal is settled once and written to the ledger without the budget."""
    if too_big(req):
        return err(413, "request too large")
    b = await body_json(req)
    pay = (b.get("payment_mandate") or {}) if isinstance(b.get("payment_mandate"), dict) else {}
    # the shop's token, or (for shops deployed without the kit) the payee's registered key on the checkout_jwt
    p = M.party(req, "shop") or M.shop_by_url(str((pay.get("mandate") or {}).get("payee") or ""))
    if p is None:
        return err(401, "join as a shop first, or name a registered shop as the payee")
    o = M.db.execute("select * from orders where id=?", (str(b.get("order")),)).fetchone()
    if o is None:
        return err(404, "no such order")
    buyer = M.db.execute("select * from parties where role='buyer' and name=?", (o["buyer"],)).fetchone()
    jwt = b.get("checkout_jwt") if isinstance(b.get("checkout_jwt"), str) else ""
    co = jwt_payload(jwt, p["pubkey"]) or {}
    cm, pm = b.get("checkout_mandate") or {}, b.get("payment_mandate") or {}
    cmm = cm.get("mandate") if isinstance(cm, dict) and isinstance(cm.get("mandate"), dict) else {}
    pmm = pm.get("mandate") if isinstance(pm, dict) and isinstance(pm.get("mandate"), dict) else {}
    opens = open_mandates(o)
    chash = b64url(hashlib.sha256(jwt.encode()).digest())
    price = co.get("price")
    econ = bool(o["category"])
    # Round 4: ask the chain first, with no state read before it, so the checks below run with no await between them.
    # Only the buyer's signature gets this far, so nobody else can make the market call the chain.
    on_chain = o["round"] >= CHAIN_ROUND
    tx_hash, fee_tx_hash, tx_problem = pmm.get("tx_hash"), pmm.get("fee_tx_hash"), None
    fee = maroo.fees(price) if type(price) is int and price >= 0 else {"shop": 0, "buyer": 0, "market": 0}
    if on_chain and buyer and verify(buyer["pubkey"], pm.get("sig") if isinstance(pm, dict) else None, pmm):
        try:
            digest = hashlib.sha256(jwt.encode()).digest()
            shop_gets = (price - fee["shop"] if type(price) is int else 0) * maroo.CREDIT_WEI
            tx_problem = await maroo.payment_problem(tx_hash, buyer["pubkey"], p["pubkey"], shop_gets, digest)
            if tx_problem is None and fee["market"]:
                tx_problem = await maroo.payment_problem(fee_tx_hash, buyer["pubkey"], M.pubkey,
                                                         fee["market"] * maroo.CREDIT_WEI, digest)
                tx_problem = tx_problem and f"market fee: {tx_problem}"
        except (httpx.HTTPError, RuntimeError, ValueError, TypeError) as e:
            print(f"[settle] chain check failed: {type(e).__name__}: {e}", flush=True)
            return err(503, "the chain did not answer; try again with the same transaction")
    elif on_chain:
        tx_problem = "not signed by the buyer"
    now = time.time()
    c = {
        "round_3_order": o["round"] >= 3,
        "not_expired": now <= o["expires"],
        "not_settled_before": M.db.execute("select 1 from settlements where order_id=?", (o["id"],)).fetchone() is None,
        "checkout_signed_by_shop": bool(co),
        "checkout_matches_order": (econ or co.get("item") == o["item"]) and type(price) is int
                                  and isinstance(co.get("task"), str),
        "checkout_mandate_signed_by_buyer": bool(buyer) and verify(buyer["pubkey"], cm.get("sig") if isinstance(cm, dict) else None, cmm),
        "payment_mandate_signed_by_buyer": bool(buyer) and verify(buyer["pubkey"], pm.get("sig") if isinstance(pm, dict) else None, pmm),
        "closed_mandates_types": cmm.get("vct") == "mandate.checkout.1" and pmm.get("vct") == "mandate.payment.1",
        "bound_to_open_mandates": cmm.get("open_mandate") == mandate_hash(opens["checkout"])
                                  and pmm.get("open_mandate") == mandate_hash(opens["payment"]),
        "bound_to_this_checkout": cmm.get("checkout_jwt") == jwt and cmm.get("checkout_hash") == chash
                                  and pmm.get("checkout_hash") == chash,
        "payee_is_this_shop": norm_url(pmm.get("payee")) == norm_url(p["url"]),
        "amount_is_checkout_total": type(price) is int and pmm.get("amount") == price,
        "amount_in_range": type(price) is int and 0 <= price <= o["budget"],
    }
    if on_chain:
        c["price_covers_fees"] = type(price) is int and price >= fee["shop"]
        c["paid_on_chain"] = tx_problem is None
        hashes = [x if isinstance(x, str) else "" for x in (tx_hash, fee_tx_hash)]
        c["transaction_not_used_before"] = M.db.execute(
            "select 1 from settlements where tx_hash in (?,?) or fee_tx_hash in (?,?)", hashes + hashes).fetchone() is None
    u = None
    if econ:
        unit_id = co.get("unit") if isinstance(co.get("unit"), str) else ""
        u = M.db.execute("select * from units where id=?", (unit_id,)).fetchone()
        cert = co.get("cert")
        claim, warranty = co.get("claim"), co.get("warranty_hours")
        c.update({
            "unit_in_stock": bool(u) and u["shop"] == p["name"] and u["status"] == "stock" and u["sku"] == co.get("item")
                             and (u["expires_at"] is None or u["expires_at"] > now),
            "item_acceptable": co.get("item") in json.loads(o["acceptable"]),
            "cert_matches_unit": cert is None or (bool(u) and isinstance(cert, dict) and canon(cert) == canon(json.loads(u["cert"]))),
            "claim_and_warranty_well_formed": claim in (None, "A", "B", "C")
                                              and (warranty is None or (type(warranty) in (int, float) and 0 <= warranty <= MAX_WARRANTY_HOURS)),
            "not_same_owner": not (p["owner"] and buyer and buyer["owner"] == p["owner"]),
        })
    if not all(c.values()):
        signed = c["checkout_mandate_signed_by_buyer"] and c["payment_mandate_signed_by_buyer"]
        shown = c if signed else {k: v for k, v in c.items() if k.endswith("_signed_by_buyer")}   # the rest is for the buyer
        out = {"settled": False, "checks": shown}
        if signed and tx_problem:
            out["chain"] = tx_problem
        return JSONResponse(out, status_code=400)
    extra, reveal = {}, {}
    if econ:
        grade = grade_now(u, now)
        offered = grade_now(u, co["iat"]) if type(co.get("iat")) in (int, float) else grade   # food ages during checkout
        info = CAT.sku.get(u["sku"], {})
        served = u["kind"] != "food" or o["use_within"] is None or (u["expires_at"] - now) / 3600 >= o["use_within"]
        extra = {"unit": u["id"], "cost": u["cost"], "grade": grade, "expires_at": u["expires_at"],
                 "claim": claim, "claim_honest": None if claim is None else int(claim in (grade, offered)),
                 "warranty_hours": warranty, "value": info.get("list_price", 0) if served else 0,
                 "use_within": o["use_within"],
                 "break_at": now + random.uniform(0, BREAK_WINDOW) if u["defect"] else None}
        M.db.execute("update units set status='sold', order_id=?, sold_at=? where id=?", (o["id"], now, u["id"]))
        M.wallet(p["name"])
        M.db.execute("update wallets set balance=balance+? where shop=?",
                     (price - fee["shop"] if on_chain else price, p["name"]))
        reveal = {"unit": u["id"], "sku": u["sku"], "name": info.get("name"), "grade": grade,
                  "expires_at": u["expires_at"], "claim": claim, "claim_honest": None if claim is None else claim in (grade, offered),
                  "warranty_hours": warranty}
    cols = {"order_id": o["id"], "ts": now, "round": o["round"], "buyer": o["buyer"], "shop_url": norm_url(p["url"]),
            "item": co.get("item") if econ else o["item"], "price": price, "task": co["task"], "checkout_hash": chash,
            **extra}
    if on_chain:
        cols.update(tx_hash=tx_hash, fee_tx_hash=fee_tx_hash if fee["market"] else None, shop_fee=fee["shop"], buyer_fee=fee["buyer"])
        reveal = dict(reveal, tx_hash=tx_hash, tx_url=maroo.tx_url(tx_hash), fee_tx_hash=cols["fee_tx_hash"],
                      shop_fee=fee["shop"], buyer_fee=fee["buyer"], market_fee=fee["market"])
    M.db.execute(f"insert into settlements({', '.join(cols)}) values({', '.join('?' * len(cols))})", list(cols.values()))
    M.db.commit()
    seq, h = M.append("settle", {"order": o["id"], "buyer": o["buyer"], "shop": norm_url(p["url"]), "item": cols["item"],
                                 "amount": price, "checkout_hash": chash, **reveal})
    receipt = {"order": o["id"], "shop": norm_url(p["url"]), "amount": price, "checkout_hash": chash, "ledger": seq, **reveal,
               **({"value": extra["value"]} if econ else {})}     # the buyer's own: did it serve the order (not on the ledger)
    return JSONResponse({"settled": True, "checks": c, "ledger": seq, "hash": h,
                         "mandate_receipt": dict(receipt, sig=base64.b64encode(M.key.sign(canon(receipt))).decode())})


async def market_key(req: Request):
    return JSONResponse({"pubkey": M.pubkey, "alg": "EIP191"})


async def chain_info(req: Request):
    return JSONResponse(dict(maroo.info(), from_round=CHAIN_ROUND, market_address=M.pubkey))


# --- economy (Round 3): catalog, supply, inventory, expiry, breakage

async def catalog(req: Request):
    return Response(CAT.catalog_json, media_type="application/json")


async def demand(req: Request):
    return Response(CAT.demand_json, media_type="application/json")


SUPPLY_LAST = {}
ROUND_AT = {}


def round_started():
    if "at" not in ROUND_AT:
        r = M.db.execute("select v from meta where k='round_at'").fetchone()
        ROUND_AT["at"] = float(r["v"]) if r else 0.0
    return ROUND_AT["at"]


def supply_window(now):
    """(opens_at, closes_at) of the restock window open now, or else the next one. The offset of each slot's window
    comes from the market key's (deterministic) signature over the slot number: unpredictable, stable across restarts."""
    if SUPPLY_EVERY <= 0:
        return now, now + SUPPLY_OPEN
    if now < round_started() + SUPPLY_FIRST:      # shops start the round with empty shelves
        return round_started(), round_started() + SUPPLY_FIRST
    k = int(now // SUPPLY_EVERY)
    while True:
        opens = k * SUPPLY_EVERY + int.from_bytes(M.key.sign(b"restock:%d" % k)[:4], "big") % SUPPLY_JITTER
        if now < opens + SUPPLY_OPEN:
            return opens, opens + SUPPLY_OPEN
        k += 1


def surge_factor(recent):
    return round(min(SURGE_MAX, 1 + max(0, recent - SURGE_FREE) * SURGE_PER_UNIT), 3)


SURGE_LIST = [0.0, []]


def surge_list(now):
    """The SKUs above their plain cost right now, cached briefly: every shop's inventory call carries it."""
    if now - SURGE_LIST[0] >= 10:
        rows = M.db.execute("select sku, count(*) from units where supplied_at>=? group by sku having count(*)>? "
                            "order by 2 desc", (now - SURGE_WINDOW, SURGE_FREE)).fetchall()
        SURGE_LIST[:] = [now, [{"sku": r[0], "name": CAT.sku.get(r[0], {}).get("name"), "units": r[1],
                                "factor": surge_factor(r[1])} for r in rows]]
    return SURGE_LIST[1]


def draw_unit(shop, info, now, factor=1.0):
    """One unit from the market's supplier: a grade, a cost in that grade's band, a shelf life or a hidden defect."""
    food = info["kind"] == "food"
    grades = FOOD_GRADES if food else GOODS_GRADES
    g = random.choices(grades, weights=[x[1] for x in grades])[0]
    lo, hi = COST_BAND[g[0]]
    uid = "u-" + secrets.token_hex(6)
    shelf = info["shelf_hours"] if food else None
    expires = now + shelf * 3600 * random.uniform(*g[2]) if food else None
    cert = {"unit": uid, "sku": info["sku"], "shop": shop, "kind": info["kind"], "supplied_at": int(now),
            "expires_at": int(expires) if food else None, "grade": None if food else g[0]}
    return {"id": uid, "shop": shop, "sku": info["sku"], "kind": info["kind"], "grade": None if food else g[0],
            "shelf_hours": shelf, "cost": int(info["list_price"] * random.uniform(lo, hi) * factor), "supplied_at": now,
            "expires_at": int(expires) if food else None, "defect": int(not food and random.random() < g[2]),
            "status": "stock", "order_id": None, "sold_at": None,
            "cert": json.dumps({"cert": cert, "sig": base64.b64encode(M.key.sign(canon(cert))).decode()})}


def unit_view(u):
    info = CAT.sku.get(u["sku"], {})
    return {"unit": u["id"], "sku": u["sku"], "name": info.get("name"), "category": info.get("category"),
            "list_price": info.get("list_price"), "kind": u["kind"], "cost": u["cost"], "supplied_at": int(u["supplied_at"]),
            "expires_at": u["expires_at"], "grade": grade_now(u), "cert": json.loads(u["cert"])}


async def supply(req: Request):
    """POST {sku, qty}: buy stock from the market. Grades and costs are drawn per unit; the total leaves the balance."""
    p = M.party(req, "shop")
    if p is None:
        return err(401, "join as a shop first")
    if M.round() < ECON_ROUND:
        return err(403, f"supply opens in Round {ECON_ROUND}")
    b = await body_json(req)
    info = CAT.sku.get(str(b.get("sku", "")))
    qty = b.get("qty")
    if info is None:
        return err(400, "unknown sku; see /api/catalog")
    if type(qty) is not int or not 1 <= qty <= SUPPLY_MAX:
        return err(400, f"qty must be a whole number from 1 to {SUPPLY_MAX}")
    now = time.time()
    opens, closes = supply_window(now)
    if now < opens:
        return JSONResponse({"error": f"supply is closed; the next restock window opens in {int(opens - now)} s",
                             "wait": round(opens - now, 1), "opens_at": int(opens), "closes_at": int(closes)},
                            status_code=429)
    if now - SUPPLY_LAST.get(p["name"], 0) < SUPPLY_GAP:
        return JSONResponse({"wait": round(SUPPLY_GAP - (now - SUPPLY_LAST[p["name"]]), 1)}, status_code=429)
    recent = M.db.execute("select count(*) from units where supplied_at>=? and sku=?",
                          (now - SURGE_WINDOW, info["sku"])).fetchone()[0]
    units = [draw_unit(p["name"], info, now, surge_factor(recent + i)) for i in range(qty)]
    total = sum(u["cost"] for u in units)
    w = M.wallet(p["name"])
    if w["balance"] < total:
        return JSONResponse({"error": f"this lot costs {total}, the balance is {w['balance']}", "balance": w["balance"],
                             "total": total}, status_code=402)
    SUPPLY_LAST[p["name"]] = now
    for u in units:
        M.db.execute(f"insert into units({', '.join(u)}) values({', '.join('?' * len(u))})", list(u.values()))
    M.db.execute("update wallets set balance=balance-? where shop=?", (total, p["name"]))
    M.db.commit()
    M.append("supply", {"shop": p["name"], "sku": info["sku"], "qty": qty, "total": total,
                        "units": [u["id"] for u in units]})
    return JSONResponse({"balance": w["balance"] - total, "total": total, "surge": surge_factor(recent + qty - 1),
                         "units": [unit_view(u) for u in units]})


async def bankruptcy(req: Request):
    """A shop below BANKRUPT_LINE files: its stock is seized, its balance goes back to the starting capital, and the
    money it was given counts against its profit. At most BANKRUPT_MAX times."""
    p = M.party(req, "shop")
    if p is None:
        return err(401, "join as a shop first")
    if M.round() < ECON_ROUND:
        return err(403, f"bankruptcy opens in Round {ECON_ROUND}")
    w = M.wallet(p["name"])
    if w["balance"] >= BANKRUPT_LINE:
        return err(409, f"the balance is {w['balance']}; filing needs less than {BANKRUPT_LINE}")
    if w["bankruptcies"] >= BANKRUPT_MAX:
        return err(409, f"this shop has already filed {BANKRUPT_MAX} times")
    seized = M.db.execute("select id, cost from units where shop=? and status='stock'", (p["name"],)).fetchall()
    M.db.execute("update units set status='seized' where shop=? and status='stock'", (p["name"],))
    given = w["start"] - w["balance"]
    M.db.execute("update wallets set balance=start, injected=injected+?, bankruptcies=bankruptcies+1 where shop=?",
                 (given, p["name"]))
    M.db.commit()
    M.append("bankrupt", {"shop": p["name"], "given": given, "seized_units": len(seized),
                          "seized_cost": sum(u["cost"] for u in seized), "filing": w["bankruptcies"] + 1})
    return JSONResponse({"balance": w["start"], "given": given, "seized_units": len(seized),
                         "bankruptcies": w["bankruptcies"] + 1, "left": BANKRUPT_MAX - w["bankruptcies"] - 1})


def restock_view(now):
    opens, closes = supply_window(now)
    return {"open": now >= opens, "opens_at": int(opens), "closes_at": int(closes)}


async def api_restock(req: Request):
    """Public: the restock window open now or next, for the landing page and anyone planning a restock."""
    return JSONResponse({"round": M.round(), "now": int(time.time()), **restock_view(time.time())})


async def api_supply_prices(req: Request):
    """Public: the surge rule and the SKUs above their plain cost."""
    return JSONResponse({"window_s": SURGE_WINDOW, "free": SURGE_FREE, "per_unit": SURGE_PER_UNIT, "max": SURGE_MAX,
                         "surge": surge_list(time.time())})


async def inventory(req: Request):
    p = M.party(req, "shop")
    if p is None:
        return err(401, "join as a shop first")
    w = M.wallet(p["name"]) if M.round() >= ECON_ROUND else None
    rows = M.db.execute("select * from units where shop=? and status='stock' order by coalesce(expires_at, supplied_at)",
                        (p["name"],)).fetchall()
    return JSONResponse({"round": M.round(), "balance": w["balance"] if w else None, "start": w["start"] if w else None,
                         "injected": w["injected"] if w else None, "bankruptcies": w["bankruptcies"] if w else None,
                         "bankruptcies_left": BANKRUPT_MAX - w["bankruptcies"] if w else None,
                         "bankrupt_line": BANKRUPT_LINE, "storage_per_day": STORAGE_PER_DAY,
                         "restock": restock_view(time.time()), "surge": surge_list(time.time()),
                         "next_dislike_fee": dislike_fee(M.db, p["owner"] or f"shop:{p['name']}", time.time()),
                         "dislike_power_unit": power_unit(M.db, p),
                         "units": [unit_view(u) for u in rows]})


def storage_fees(now):
    """Every STORAGE_EVERY, each shop pays STORAGE_PER_DAY of the cost of the goods it holds, pro rata."""
    for w in M.db.execute("select * from wallets").fetchall():
        last = w["fee_at"] or w["created"]
        if now - last < STORAGE_EVERY:
            continue
        held = M.db.execute("select coalesce(sum(cost), 0) from units where shop=? and status='stock' and kind='goods'",
                            (w["shop"],)).fetchone()[0]
        fee = int(held * STORAGE_PER_DAY * (now - last) / 86400)
        M.db.execute("update wallets set balance=balance-?, fee_at=? where shop=?", (fee, now, w["shop"]))
        M.db.commit()
        if fee:
            M.append("storage", {"shop": w["shop"], "goods_cost": held, "fee": fee})


def econ_sweep():
    """Write off stale food, break defective goods, refund under warranty, charge storage."""
    now = time.time()
    storage_fees(now)
    gone = M.db.execute("select * from units where status='stock' and expires_at is not null and expires_at<=?",
                        (now,)).fetchall()
    by_shop = {}
    for u in gone:
        by_shop.setdefault(u["shop"], []).append(u)
    for shop, us in by_shop.items():
        M.db.execute(f"update units set status='expired' where id in ({','.join('?' * len(us))})", [u["id"] for u in us])
        M.db.commit()
        M.append("expire", {"shop": shop, "units": [u["id"] for u in us], "skus": [u["sku"] for u in us],
                            "cost": sum(u["cost"] for u in us)})
    for s in M.db.execute("select * from settlements where break_at is not null and break_at<=? and broke=0",
                          (now,)).fetchall():
        refund = bool(s["warranty_hours"]) and s["break_at"] - s["ts"] <= s["warranty_hours"] * 3600
        shop = M.shop_by_url(s["shop_url"])
        M.db.execute("update settlements set broke=1, value=0, refunded=? where order_id=?", (int(refund), s["order_id"]))
        if refund and shop:
            M.wallet(shop["name"])
            M.db.execute("update wallets set balance=balance-? where shop=?", (s["price"], shop["name"]))
        M.db.commit()
        M.append("broken", {"order": s["order_id"], "buyer": s["buyer"], "shop": s["shop_url"], "unit": s["unit"],
                            "item": s["item"], "hours_after_sale": round((s["break_at"] - s["ts"]) / 3600, 2),
                            "warranty_hours": s["warranty_hours"], "refunded": s["price"] if refund else 0})


async def econ_loop():
    while True:
        await asyncio.sleep(ECON_EVERY)
        try:
            econ_sweep()
        except Exception as e:                       # one bad row must not stop the sweeps
            print(f"econ sweep: {type(e).__name__}: {e}", flush=True)


# --- results and reviews

def check_result(buyer, b):
    """Every Round-2 check, computed for every result so the dashboard can show what failed."""
    rec = b.get("receipt") or {}
    body = {k: rec.get(k) for k in RECEIPT_KEYS}
    o = M.db.execute("select * from orders where id=?", (b.get("order"),)).fetchone()
    shop = M.shop_by_url(b.get("shop"))
    seen = M.db.execute("select 1 from results where json_extract(receipt,'$.task')=? and receipt is not null",
                        (body["task"],)).fetchone() if body["task"] else None
    c = {
        "order_issued_to_buyer": bool(o and o["buyer"] == buyer["name"]),
        "one_result_per_order": bool(o and not o["done"]),
        "item_matches": bool(o and (body["item"] == o["item"]
                                    or (o["acceptable"] and body["item"] in json.loads(o["acceptable"])))),
        "price_within_budget": bool(o and isinstance(body["price"], int) and body["price"] <= o["budget"]),
        "task_not_seen": bool(body["task"]) and seen is None,
        "sig_seller_ok": bool(shop and rec.get("sig_seller") and verify(shop["pubkey"], rec["sig_seller"], body)),
        "sig_buyer_ok": bool(b.get("sig_buyer") and verify(buyer["pubkey"], b["sig_buyer"],
                                                               dict(body, order=b.get("order")))),
    }
    if o and o["round"] >= 3:
        s = M.db.execute("select * from settlements where order_id=?", (o["id"],)).fetchone()
        c["settled"] = bool(s and s["shop_url"] == norm_url(b.get("shop")) and s["price"] == body["price"]
                            and s["task"] == body["task"])
    return c, o


async def result(req: Request):
    """One buyer's outcome: {order, shop, receipt|null, stars, review, sig_buyer}."""
    p = M.party(req, "buyer")
    if p is None:
        return err(401, "join as a buyer first")
    if too_big(req):
        return err(413, "request too large")
    b = await body_json(req)
    rnd = M.round()
    rec = b.get("receipt")
    if rec is not None and not isinstance(rec, dict):
        return err(400, "receipt must be an object or null")
    if rec and isinstance(rec.get("price"), float) and rec["price"].is_integer():
        rec["price"] = int(rec["price"])
    if rec and not (type(rec.get("price")) is int and 0 <= rec["price"] <= 10 ** 9
                    and all(isinstance(rec.get(k), str) and len(rec[k]) <= 200 for k in ("shop", "item", "task"))):
        return err(400, "receipt needs shop, item and task as strings and price as a whole number")
    checks, o = check_result(p, b) if rec else ({}, M.db.execute("select * from orders where id=?",
                                                                  (b.get("order"),)).fetchone())
    verified = int(bool(rec) and all(checks.values()))
    stars = b.get("stars")
    stars = stars if isinstance(stars, int) and 1 <= stars <= 5 else None
    text = str(b.get("review") or "")[:400]
    cur = M.db.execute("insert into results(ts, round, buyer, order_id, shop_url, receipt, stars, review, checks, verified)"
                       " values(?,?,?,?,?,?,?,?,?,?)",
                       (time.time(), rnd, p["name"], b.get("order"), norm_url(b.get("shop")),
                        json.dumps(rec, ensure_ascii=False) if rec else None, stars, text,
                        json.dumps(checks), verified))
    rid = cur.lastrowid
    if o and o["buyer"] == p["name"]:
        M.db.execute("update orders set done=1 where id=?", (o["id"],))
    review_ok = stars is not None and (rnd == 1 or (rnd >= 2 and verified))
    if review_ok:
        M.db.execute("insert into reviews(ts, round, author, shop_url, stars, text, result_id, verified)"
                     " values(?,?,?,?,?,?,?,?)",
                     (time.time(), rnd, p["name"], norm_url(b.get("shop")), stars, text, rid, verified))
    M.db.commit()
    seq, h = M.append("result", {"id": rid, "buyer": p["name"], "order": b.get("order"), "shop": norm_url(b.get("shop")),
                                 "receipt": rec, "sig_buyer": b.get("sig_buyer"), "stars": stars, "review": text,
                                 "checks": checks, "verified": bool(verified)})
    return JSONResponse({"result": rid, "ledger": seq, "hash": h, "round": rnd, "checks": checks,
                         "verified": bool(verified), "review_accepted": review_ok})


async def review(req: Request):
    """A review without a result. Round 1 takes it from anyone; Round 2 refuses it."""
    p = M.party(req)
    if p is None:
        return err(401, "join first")
    if too_big(req):
        return err(413, "request too large")
    rnd = M.round()
    b = await body_json(req)
    stars = b.get("stars")
    if rnd != 1:
        return err(403, "reviews without a trade are taken only in Round 1")
    if not (isinstance(stars, int) and 1 <= stars <= 5) or not M.shop_by_url(b.get("shop")):
        return err(400, "need shop (a registered card URL) and stars 1..5")
    text = str(b.get("review") or "")[:400]
    M.db.execute("insert into reviews(ts, round, author, shop_url, stars, text, result_id, verified)"
                 " values(?,?,?,?,?,?,null,0)", (time.time(), rnd, p["name"], norm_url(b["shop"]), stars, text))
    M.db.commit()
    M.append("review", {"author": p["name"], "shop": norm_url(b["shop"]), "stars": stars, "review": text})
    return JSONResponse({"accepted": True})


# --- forum: any agent posts, any agent votes; no content rules, one post per FORUM_GAP

FORUM_LAST = {}


def ref_view(seq):
    r = M.db.execute("select seq, kind, data from ledger where seq=?", (seq,)).fetchone()
    if r is None:
        return {"seq": seq, "found": False}
    d = json.loads(r["data"])
    keep = ("order", "buyer", "shop", "item", "amount", "unit", "name", "grade", "claim", "claim_honest",
            "warranty_hours", "refunded", "hours_after_sale", "qty", "total", "stars", "review", "verified")
    return {"seq": seq, "found": True, "kind": r["kind"], "data": {k: d[k] for k in keep if k in d}}


def post_score(likes, dislikes):
    """Wilson lower bound (95%) of the like ratio: few votes stay near zero."""
    n = likes + dislikes
    if n == 0:
        return None
    z, phat = 1.96, likes / n
    return round((phat + z * z / (2 * n) - z * ((phat * (1 - phat) + z * z / (4 * n)) / n) ** 0.5) / (1 + z * z / n), 3)


def settled_count(db, name, role):
    col = "buyer=?" if role == "buyer" else "shop_url=(select url from parties where role='shop' and name=?)"
    return db.execute(f"select count(*) from settlements where {col}", (name,)).fetchone()[0]


VOTERS, NOT_YET = set(), {}   # settled deals never go down: who qualified stays qualified, who did not is asked again soon


def can_vote(db, name, role):
    key = (role, name)
    if key in VOTERS:
        return True
    now = time.time()
    if now < NOT_YET.get(key, 0):
        return False
    if settled_count(db, name, role) >= VOTE_MIN_DEALS:
        VOTERS.add(key)
        return True
    NOT_YET[key] = now + 10
    return False


async def forum_post(req: Request):
    p = M.party(req)
    if p is None:
        return err(401, "join first")
    if too_big(req):
        return err(413, "request too large")
    b = await body_json(req)
    text = b.get("text")
    if not isinstance(text, str) or not text.strip():
        return err(400, "text is required")
    if len(text) > FORUM_MAX:
        return err(400, f"at most {FORUM_MAX} characters")
    key, now = f"{p['role']}:{p['name']}", time.time()
    if now - FORUM_LAST.get(key, 0) < FORUM_GAP:
        return JSONResponse({"wait": round(FORUM_GAP - (now - FORUM_LAST[key]), 1)}, status_code=429)
    refs = b.get("refs") or []
    if not isinstance(refs, list) or len(refs) > 10 or not all(type(x) is int and 0 < x < 2 ** 53 for x in refs):
        return err(400, "refs: up to 10 ledger sequence numbers")
    thread = b.get("thread")
    if thread is not None and (type(thread) is not int
                               or not M.db.execute("select 1 from forum where id=?", (thread,)).fetchone()):
        return err(400, "thread must be the id of an existing post")
    FORUM_LAST[key] = now
    cur = M.db.execute("insert into forum(ts, round, author, role, owner, thread, text, refs) values(?,?,?,?,?,?,?,?)",
                       (now, M.round(), p["name"], p["role"], p["owner"], thread, text, json.dumps(refs)))
    M.db.commit()
    return JSONResponse({"post": cur.lastrowid})


async def forum_vote(req: Request):
    p = M.party(req)
    if p is None:
        return err(401, "join first")
    b = await body_json(req)
    post = M.db.execute("select * from forum where id=?", (b.get("post"),)).fetchone() if type(b.get("post")) is int else None
    if post is None:
        return err(404, "no such post")
    value = b.get("value")
    if type(value) is not int or not (value == 1 or -POWER_MAX <= value <= -1):
        return err(400, f"value must be 1 (like), -1 (dislike) or -N (dislike with N votes on the author, N up to {POWER_MAX})")
    power = -value if value < -1 else None
    if post["author"] == p["name"] and post["role"] == p["role"]:
        return err(400, "agents cannot vote on their own posts")
    counts = not (p["owner"] and p["owner"] == post["owner"]) and can_vote(M.db, p["name"], p["role"])
    voter, now, fee = f"{p['role']}:{p['name']}", time.time(), None
    account = p["owner"] or voter
    same_owner = bool(p["owner"]) and p["owner"] == post["owner"]
    priced = (not same_owner and value < 0 and post["role"] == "buyer" and not post["hidden"] and post["round"] == M.round()
              and M.round() >= ECON_ROUND)
    if power and not priced:
        return err(400, "more than one vote works only on a visible buyer post of this round, from another account")
    if power and not counts:
        return err(403, f"more than one vote needs {VOTE_MIN_DEALS} settled deals")
    extra = {}
    if priced and now >= DISLIKE_FEE_FROM:                                 # a voter short of the deals pays too: its vote counts later
        before = stance_on(M.db, p, post)
        old = M.db.execute("select value from votes where post=? and voter=?", (post["id"], voter)).fetchone()
        after = before - (old["value"] if old else 0) - 1
        opens = before >= 0 and after < 0                                  # this vote opens a dislike on the author
        open_fee = dislike_fee(M.db, account, now) if opens else 0
        held = current_power(M.db, post["round"], account, post["author"])
        if power and after >= 0:
            return err(400, "your likes on this author outweigh this dislike, so it holds no dislike to add votes to")
        unit = power_unit(M.db, p)
        power_fee = unit * ((power - 1) ** 2 - (held - 1) ** 2) if power and power > held else 0
        fee = open_fee + power_fee
        if fee:
            w = paying_wallet(M.db, p)
            if w is None or w["balance"] < fee:
                return JSONResponse({"error": f"this dislike costs {fee}, paid from your shop's cash", "fee": fee,
                                     "balance": w["balance"] if w else None}, status_code=402)
            M.db.execute("update wallets set balance=balance-? where shop=?", (fee, w["shop"]))
        if opens:
            M.db.execute("insert into dislike_fees values(?,?,?,?)", (now, account, post["author"], open_fee))
        if power_fee:
            M.db.execute("insert into dislike_power values(?,?,?,?,?,?) on conflict(round, account, author)"
                         " do update set power=excluded.power, paid=paid+excluded.paid, ts=excluded.ts",
                         (post["round"], account, post["author"], power, power_fee, now))
            held = power
        extra = {"votes_on_author": held, "power_fee": power_fee, "next_vote_fee": power_unit(M.db, p) * ((held) ** 2 - (held - 1) ** 2)}
    M.db.execute("insert or replace into votes values(?,?,?,?,?)", (post["id"], voter, p["owner"], -1 if value < 0 else 1, now))
    M.db.commit()
    return JSONResponse({"ok": True, "counted": counts, "fee": fee or 0, "next_dislike_fee": dislike_fee(M.db, account, now), **extra})


def stance_on(db, p, post):
    """Likes minus dislikes from the voter's account on one author's posts this round, as forum_ratings counts them:
    the account's other agents count only with enough deals, so a new sibling's like cannot hide a dislike from the fee."""
    me = f"{p['role']}:{p['name']}"
    who, val = ("v.owner=?", p["owner"]) if p["owner"] else ("v.voter=?", me)
    total = 0
    for v in db.execute(f"select v.voter, sum(v.value) as net from votes v join forum f on f.id=v.post where {who}"
                        " and f.role=? and f.author=? and f.round=? and f.hidden=0 group by v.voter",
                        (val, post["role"], post["author"], post["round"])):
        role, name = v["voter"].split(":", 1)
        if v["voter"] == me or can_vote(db, name, role):
            total += v["net"]
    return total


def dislike_fee(db, account, now):
    """What opening one more dislike costs this account right now."""
    if now < DISLIKE_FEE_FROM:
        return 0
    recent = db.execute("select count(*) from dislike_fees where account=? and ts>?", (account, now - DISLIKE_WINDOW)).fetchone()[0]
    return DISLIKE_STEP * max(0, recent + 1 - DISLIKE_FREE)


def current_power(db, rnd, account, author):
    """How many votes this account holds on a buyer while it dislikes them: 1 until it pays for more."""
    row = db.execute("select power from dislike_power where round=? and account=? and author=?", (rnd, account, author)).fetchone()
    return row["power"] if row else 1


def power_unit(db, p):
    """The price of the first extra vote: a share of the account's largest Book, so a rich account pays as much in relative terms."""
    if p["owner"]:
        where, val = "s.owner=?", p["owner"]
    elif p["role"] == "shop":
        where, val = "s.name=?", p["name"]
    else:
        return DISLIKE_STEP
    row = db.execute("select max(w.balance - w.start - w.injected + coalesce((select sum(u.cost) from units u where u.shop=w.shop"
                     f" and u.status='stock'), 0)) as book from wallets w join parties s on s.name=w.shop and s.role='shop' where {where}",
                     (val,)).fetchone()
    return max(DISLIKE_STEP, int(POWER_RATE * (row["book"] or 0)))


def paying_wallet(db, p):
    """A shop pays from its own cash; a buyer from the richest shop of its account. None without a shop."""
    if p["role"] == "shop":
        return db.execute("select * from wallets where shop=?", (p["name"],)).fetchone()
    if not p["owner"]:
        return None
    return db.execute("select w.* from wallets w join parties s on s.name=w.shop and s.role='shop' where s.owner=?"
                      " order by w.balance desc limit 1", (p["owner"],)).fetchone()


def forum_ratings(db, rnd):
    """(role, author) -> rating. Each account holds one stance on an author: its likes minus its dislikes there.
    A like stance weighs 1 + log2(net likes), a dislike stance weighs the votes the account paid for on that buyer (1 unpaid),
    an account without an owner weighs 1 on likes."""
    nets, plain, eligible = {}, set(), {}
    for v in db.execute("select v.voter, v.owner, f.role, f.author, f.owner as author_owner, sum(v.value) as net"
                        " from votes v join forum f on f.id=v.post where f.round=? and f.hidden=0"
                        " group by v.voter, v.owner, f.role, f.author, f.owner", (rnd,)):
        if v["owner"] and v["owner"] == v["author_owner"]:
            continue
        if v["voter"] not in eligible:
            role, name = v["voter"].split(":", 1)
            eligible[v["voter"]] = can_vote(db, name, role)
        if not eligible[v["voter"]]:
            continue
        account = v["owner"] or v["voter"]
        if not v["owner"]:
            plain.add(account)
        one = nets.setdefault((v["role"], v["author"]), {})
        one[account] = one.get(account, 0) + v["net"]
    powers = {(r["account"], r["author"]): r["power"] for r in db.execute("select account, author, power from dislike_power where round=?", (rnd,))}
    out = {}
    for key, stances in nets.items():
        likes = sum(1 if a in plain else 1 + math.log2(x) for a, x in stances.items() if x > 0)
        out[key] = post_score(likes, sum(powers.get((a, key[1]), 1) if key[0] == "buyer" else 1 for a, x in stances.items() if x < 0))
    return out


def star_voices(db, rnd, cm):
    """shop url -> one mean per reviewing account."""
    acc = {}
    for r in db.execute("select coalesce(nullif(p.owner, ''), r.author) as account, r.shop_url, sum(r.stars) as total, count(*) as n"
                        " from reviews r left join parties p on p.name=r.author and p.role='buyer' where r.round=?"
                        " group by account, r.shop_url", (rnd,)):
        t = acc.setdefault((cm.get(r["shop_url"], r["shop_url"]), r["account"]), [0, 0])
        t[0] += r["total"]
        t[1] += r["n"]
    out = {}
    for (url, _), (total, n) in acc.items():
        out.setdefault(url, []).append(total / n)
    return out


def saved_of(mine):
    """Average share of list price kept per deal, pulled toward 0 for few deals: a sofa and a snack at 30% off count the same."""
    total = 0.0
    for x in mine:
        value = x["value"] or 0
        total += max(-1.0, (value - x["price"] - (x["buyer_fee"] or 0) + (x["price"] if x["refunded"] else 0)) / (value or max(x["price"], 1)))
    return round(total / (len(mine) + SAVED_PRIOR), 4)


def vote_tally(db, post_ids):
    """post id -> (likes, dislikes), counting only eligible voters."""
    if not post_ids:
        return {}
    marks = ",".join("?" * len(post_ids))
    eligible = {}
    out = {}
    for v in db.execute(f"select v.*, f.owner as author_owner from votes v join forum f on f.id=v.post"
                          f" where v.post in ({marks})", list(post_ids)):
        if v["owner"] and v["owner"] == v["author_owner"]:
            continue
        if v["voter"] not in eligible:
            role, name = v["voter"].split(":", 1)
            eligible[v["voter"]] = can_vote(db, name, role)
        if not eligible[v["voter"]]:
            continue
        l, d = out.get(v["post"], (0, 0))
        out[v["post"]] = (l + (v["value"] == 1), d + (v["value"] == -1))
    return out


async def forum(req: Request):
    try:
        since = int(req.query_params.get("since", "0"))
        limit = max(1, min(int(req.query_params.get("limit", str(FORUM_PAGE))), FORUM_PAGE))
    except ValueError:
        return err(400, "since and limit must be numbers")
    if since > 0:
        rows = M.db.execute("select * from forum where id>? and hidden=0 order by id limit ?", (since, limit)).fetchall()
    else:
        rows = M.db.execute("select * from forum where hidden=0 order by id desc limit ?", (limit,)).fetchall()[::-1]
    tally = vote_tally(M.db, [r["id"] for r in rows])
    out = []
    for r in rows:
        l, d = tally.get(r["id"], (0, 0))
        out.append({"id": r["id"], "ts": round(r["ts"]), "round": r["round"], "author": r["author"], "role": r["role"],
                    "owner": r["owner"], "thread": r["thread"], "text": r["text"],
                    "refs": [ref_view(s) for s in json.loads(r["refs"] or "[]")],
                    "likes": l, "dislikes": d, "score": post_score(l, d)})
    return JSONResponse(out)


def market_post(text):
    M.db.execute("insert into forum(ts, round, author, role, owner, thread, text, refs) values(?,?,?,?,?,?,?,?)",
                 (time.time(), M.round(), "market", "market", None, None, text, "[]"))
    M.db.commit()


# --- dashboard

def item_label(item):
    """A SKU shows as its catalog category (소분류); Round 0-2 items and order names stay as they are."""
    info = CAT.sku.get(item) if isinstance(item, str) else None
    return info["category"].split(">")[-1] if info else item


def counted(r, rnd):
    return r["receipt"] is not None and (rnd < 2 or r["verified"])


DONE_ROUNDS = {}           # (round, reveal, names and labels) -> stats of a round that is over; it no longer changes


def stats(reveal=False):
    db = reader()
    cur = int(db.execute("select v from meta where k='round'").fetchone()["v"])
    sh = db.execute("select name, url, label from parties where role='shop'").fetchall()
    names = {r["url"]: r["name"] for r in sh}
    labels = {r["url"]: r["label"] for r in sh}
    sig = hash((tuple(sorted(names.items())), tuple(sorted((k, v or "") for k, v in labels.items()))))
    out = {"round": cur, "rounds": {}}
    rounds = {r[0] for r in db.execute("select distinct round from results")} | {cur}
    for rnd in sorted(rounds):
        key = (rnd, reveal, sig)
        if rnd < cur and key in DONE_ROUNDS:
            out["rounds"][rnd] = DONE_ROUNDS[key]
            continue
        out["rounds"][rnd] = round_stats(db, rnd, names, labels, reveal, canon_urls(db))
        if rnd < cur:
            DONE_ROUNDS[key] = out["rounds"][rnd]
    return out


def round_stats(db, rnd, names, labels, reveal, cm):
    rr = db.execute("select order_id, shop_url, receipt, verified from results where round=? order by id", (rnd,)).fetchall()
    n_deals, per_item, agg = 0, {}, {}
    for r in rr:                                     # one pass over the round, grouped by shop
        a = agg.setdefault(cm.get(r["shop_url"], r["shop_url"]), {"deals": 0, "receipts": 0, "with_order": 0, "visits": 0, "stars": []})
        if r["order_id"]:
            a["visits"] += 1
        if r["receipt"]:
            a["receipts"] += 1
            a["with_order"] += bool(r["order_id"])
        if counted(r, rnd):
            n_deals += 1
            a["deals"] += 1
            rec = json.loads(r["receipt"])
            if isinstance(rec.get("price"), (int, float)):
                per_item.setdefault(item_label(rec.get("item")), []).append(rec["price"])
    for x in db.execute("select shop_url, stars from reviews where round=?", (rnd,)):
        agg.setdefault(cm.get(x["shop_url"], x["shop_url"]), {"deals": 0, "receipts": 0, "with_order": 0, "visits": 0, "stars": []})["stars"].append(x["stars"])
    shops_out = []
    for url in sorted(agg):
        a, sv = agg[url], agg[url]["stars"]
        s = {"shop": names.get(url, url), "url": url,
             "deals": a["deals"],
             "share": round(a["deals"] / n_deals, 3) if n_deals else 0,
             "receipts": a["receipts"],
             "receipts_with_order": a["with_order"],
             "visits": a["visits"],
             "reviews": len(sv), "stars": round(sum(sv) / len(sv), 2) if sv else None}
        if reveal:
            s["label"] = labels.get(url)
        shops_out.append(s)
    return {
        "results": len(rr), "deals": n_deals,
        "walkaways": sum(1 for r in rr if r["receipt"] is None),
        "items": {k: {"n": len(v), "min": min(v), "max": max(v), "mean": round(sum(v) / len(v), 1)}
                  for k, v in sorted(per_item.items(), key=lambda kv: str(kv[0]))},
        "shops": sorted(shops_out, key=lambda s: -s["deals"]),
    }


CACHE, WANTED, COST = {}, {}, {}
REFRESH_EVERY = 5          # seconds between background recomputes of a view
WANTED_FOR = 600           # stop refreshing a view nobody has asked for in this long
_local = threading.local()


def reader():
    """A read-only connection per worker thread, so views never touch the writer's connection."""
    if getattr(_local, "db", None) is None:
        _local.db = sqlite3.connect(f"file:{M.path}?mode=ro", uri=True, timeout=10)
        _local.db.row_factory = sqlite3.Row
    return _local.db


async def snapshot(key, fn):
    """Serve the last computed view; refresh_loop recomputes it off the event loop."""
    WANTED[key] = (time.time(), fn)
    hit = CACHE.get(key)
    if hit is None:                                  # first request for this view: compute once, in a thread
        t = time.time()
        hit = CACHE[key] = (time.time(), await asyncio.get_running_loop().run_in_executor(pool_for(key), gated(key, fn)))
        COST[key] = time.time() - t
    return hit[1]


async def refresh_loop():
    while True:
        await asyncio.sleep(REFRESH_EVERY)
        for key, (seen, fn) in list(WANTED.items()):
            if time.time() - seen > WANTED_FOR:
                WANTED.pop(key, None)
                CACHE.pop(key, None)
                continue
            if time.time() - CACHE.get(key, (0,))[0] < max(REFRESH_EVERY, 2 * COST.get(key, 0)):
                continue                             # a view that takes T seconds rests at least 2T between runs
            with contextlib.suppress(Exception):
                t = time.time()
                CACHE[key] = (time.time(), await asyncio.get_running_loop().run_in_executor(pool_for(key), gated(key, fn)))
                COST[key] = time.time() - t


def checkpoint():
    """Copy the WAL into the database without blocking anyone; the writer reuses the WAL from the start once
    no reader needs its old pages, and journal_size_limit then trims the file."""
    c = sqlite3.connect(M.path, timeout=1)
    try:
        done = c.execute("pragma wal_checkpoint(PASSIVE)").fetchone()
        wal = Path(M.path + "-wal")
        if wal.exists() and wal.stat().st_size > 256 * 2**20 and done[1] == done[2]:   # only once PASSIVE copied it all:
            c.execute("pragma busy_timeout=200")   # TRUNCATE holds the write lock while it copies what is left
            with SNAP_GATE:                         # no board or stats read is open while it runs
                done = c.execute("pragma wal_checkpoint(PASSIVE)").fetchone()   # what came in while waiting for the gate
                t = time.time()
                if done[1] == done[2]:
                    done = c.execute("pragma wal_checkpoint(TRUNCATE)").fetchone()
                    print(f"wal truncated in {time.time() - t:.2f}s: {tuple(done)}", flush=True)
        return done
    finally:
        c.close()


LOOP_BEAT = [time.time()]
sys.setswitchinterval(0.0005)    # the snapshot threads hold the GIL; each sqlite row the event loop reads waits for it


async def beat_loop():
    while True:
        LOOP_BEAT[0] = time.time()
        await asyncio.sleep(0.2)


def loop_watchdog(main_id):
    """Log what the event loop is running when it stalls for more than 2 s (once per stall)."""
    told = False
    while True:
        time.sleep(0.5)
        lag = time.time() - LOOP_BEAT[0]
        if lag > 2 and not told:
            frame = sys._current_frames().get(main_id)
            stack = "".join(traceback.format_stack(frame)[-8:]) if frame else "?"
            print(f"event loop stalled {lag:.1f}s in:\n{stack}", flush=True)
            told = True
        elif lag <= 2:
            told = False


async def checkpoint_loop():
    while True:
        await asyncio.sleep(30)
        with contextlib.suppress(Exception):
            await asyncio.get_running_loop().run_in_executor(CKPT_POOL, checkpoint)


async def api_stats(req: Request):
    reveal = admin_ok(req)
    return JSONResponse(await snapshot(("stats", reveal), lambda: stats(reveal=reveal)))


READ_POOL = ThreadPoolExecutor(4, thread_name_prefix="read")    # many-row reads stay off the event loop


async def off_loop(fn, *args):
    return await asyncio.get_running_loop().run_in_executor(READ_POOL, fn, *args)


async def trades(req: Request):
    """Every result in order, for the dashboard's charts."""
    try:
        after = int(req.query_params.get("after", "0"))
    except ValueError:
        return err(400, "after must be a number")
    return JSONResponse(await off_loop(trade_rows, after))


def trade_rows(after):
    db = reader()
    names = {r["url"]: r["name"] for r in db.execute("select url, name from shop_urls")}
    out = []
    top = db.execute("select max(id) from results").fetchone()[0] or 0
    if after <= 0 or after < top - 2000:             # first load (or far behind): the recent trades are enough for the chart
        after = max(0, top - 400)
    for r in db.execute("select * from results where id>? order by id limit 500", (after,)):
        rec = json.loads(r["receipt"]) if r["receipt"] else None
        out.append({"id": r["id"], "ts": round(r["ts"], 1), "round": r["round"], "shop": names.get(r["shop_url"], r["shop_url"]),
                    "item": item_label(rec.get("item")) if rec else None, "price": rec.get("price") if rec else None,
                    "order": bool(r["order_id"]), "verified": bool(r["verified"]), "counted": counted(r, r["round"]),
                    "stars": r["stars"]})
    return out


async def ledger(req: Request):
    try:
        after = int(req.query_params.get("after", "0"))
    except ValueError:
        return err(400, "after must be a number")
    return JSONResponse(await off_loop(ledger_rows, after))


def ledger_rows(after):
    db = reader()
    if after < 0:                                    # after=-N: the last N entries
        after = max(0, (db.execute("select max(seq) from ledger").fetchone()[0] or 0) + after)
    rows = db.execute("select * from ledger where seq>? order by seq limit 500", (after,)).fetchall()
    return [{"seq": r["seq"], "ts": r["ts"], "kind": r["kind"], "round": r["round"],
             "data": json.loads(r["data"]), "prev": r["prev"], "hash": r["hash"]} for r in rows]


def hosted(name):
    """A served file, its links pointing at this market when MARKET_BASE_URL is set."""
    text = (Path(__file__).parent / name).read_text()
    return text.replace("https://market.ourolabs.ai", BASE_URL) if BASE_URL else text


def page(name):
    async def serve(req: Request):
        return HTMLResponse(hosted(name))
    return serve


dashboard = page("dashboard.html")
landing_page = page("landing.html")


async def landing(req: Request):
    if "admin" in req.query_params:                  # old instructor links: /?admin=<token>
        return RedirectResponse("/dashboard?" + str(req.query_params), status_code=307)
    return await landing_page(req)


async def install(req: Request):
    return Response(hosted("install.sh"), media_type="text/plain; charset=utf-8")


async def agent_md(req: Request):
    return Response(hosted("agent.md"), media_type="text/markdown; charset=utf-8")


async def kit(req: Request):
    """The kit, the market test and the starter, packed by the deploy script."""
    f = Path(__file__).parent / "kit.tar.gz"
    if not f.exists():
        return err(404, "the kit is not packed on this market")
    return Response(f.read_bytes(), media_type="application/gzip",
                    headers={"content-disposition": "attachment; filename=market-kit.tar.gz"})


# --- leaderboard

def board(rnd):
    db = reader()
    users = {r["login"]: {"login": r["login"], "avatar": r["avatar"], "html": r["html"]}
             for r in db.execute("select login, avatar, html from users")}
    parties = db.execute("select * from parties").fetchall()
    shop_of = {p["url"]: p for p in parties if p["role"] == "shop"}
    cm = canon_urls(db)
    here = lambda u: cm.get(u, u)
    rows = db.execute("select r.*, o.budget, o.item as o_item from results r left join orders o on o.id=r.order_id"
                      " where r.round=? order by r.id", (rnd,)).fetchall()
    revs = db.execute("select author, shop_url, stars, text, ts from reviews where round=?"
                      + (" and verified=1" if rnd >= 2 else "") + " order by id", (rnd,)).fetchall()
    now = time.time()
    last_order = {r["buyer"]: r["t"] for r in db.execute("select buyer, max(issued) t from orders group by buyer")}
    n_deals, rejected = 0, 0
    deals_of, visits_of, stars_of, by_buyer = {}, {}, {}, {}
    for r in rows:                                   # one pass over the round, grouped by shop and by buyer
        if counted(r, rnd):
            n_deals += 1
            deals_of.setdefault(here(r["shop_url"]), []).append(r)
        elif r["receipt"] is not None:
            rejected += 1
        if r["order_id"]:
            visits_of[here(r["shop_url"])] = visits_of.get(here(r["shop_url"]), 0) + 1
            by_buyer.setdefault(r["buyer"], []).append(r)
    for x in revs:
        stars_of.setdefault(here(x["shop_url"]), []).append(x["stars"])
    owner_of = {p["name"]: users.get(p["owner"]) for p in parties if p["role"] == "buyer"}

    def rev(x):
        return {"author": x["author"], "owner": owner_of.get(x["author"]), "stars": x["stars"],
                "review": x["text"], "ts": round(x["ts"])}

    def who(p):
        return {"name": p["name"], "owner": users.get(p["owner"]), "house": p["label"] is not None,
                "joined": round(p["joined"])}

    shops = []
    for p in shop_of.values():
        mine = deals_of.get(p["url"], [])
        sv = stars_of.get(p["url"], [])
        recs = [json.loads(r["receipt"]) for r in mine]
        prices = [x.get("price") for x in recs]
        shops.append(dict(who(p), card=json.loads(p["card"]) if p["card"] else None,
                          alive=now - (p["alive"] or 0) < ALIVE_FOR, deals=len(mine),
                          share=round(len(mine) / n_deals, 3) if n_deals else 0,
                          visits=visits_of.get(p["url"], 0),
                          stars=round(sum(sv) / len(sv), 2) if sv else None, reviews=len(sv),
                          revenue=sum(x for x in prices if isinstance(x, (int, float))),
                          profit=sum(x["price"] - RESERVE[x["item"]] for x in recs                # the floor stands in for cost
                                     if isinstance(x.get("price"), (int, float)) and x.get("item") in RESERVE)))
    buyers = []
    for p in parties:
        if p["role"] != "buyer":
            continue
        mine = by_buyer.get(p["name"], [])
        got = [r for r in mine if counted(r, rnd) and r["budget"]]
        paid = [json.loads(r["receipt"]).get("price") for r in got]
        ratios = [x / r["budget"] for x, r in zip(paid, got) if isinstance(x, (int, float))]
        saved = sum(r["budget"] - x for x, r in zip(paid, got) if isinstance(x, (int, float)))
        hopeless = [r for r in mine if r["budget"] and r["o_item"] in RESERVE and r["budget"] < RESERVE[r["o_item"]]]
        buyers.append(dict(who(p), alive=now - last_order.get(p["name"], 0) < 600, orders=len(mine), deals=len(got),
                           paid=round(sum(ratios) / len(ratios), 3) if ratios else None, saved=round(saved),
                           held=sum(1 for r in hopeless if r["receipt"] is None), hopeless=len(hopeless)))
    players = econ_board(db, rnd, shops, buyers, shop_of, stars_of) if rnd >= ECON_ROUND else []
    shops.sort(key=lambda s: (-s["deals"], -(s["stars"] or 0)))
    buyers.sort(key=lambda b: (-b["deals"], b["paid"] if b["paid"] is not None else 9))
    return {"round": rnd, "current": int(db.execute("select v from meta where k='round'").fetchone()["v"]),
            "shops": shops, "buyers": buyers, "players": players,
            "reviews": [dict(rev(x), shop=shop_of[here(x["shop_url"])]["name"] if here(x["shop_url"]) in shop_of else x["shop_url"])
                        for x in reversed(revs[-BOARD_REVIEWS:])],
            "totals": {"agents_online": sum(1 for s in shops if s["alive"]) + sum(1 for b in buyers if b["alive"]),
                       "github_players": len({p["owner"] for p in parties if p["owner"]}),
                       "results": len(rows), "deals": n_deals,
                       "rejected": rejected,
                       "walkaways": sum(1 for r in rows if r["receipt"] is None)},
            "github": bool(GH_ID)}


def percentile(rows, key):
    """Each row's share of the others it beats (ties count half), 0..1."""
    vals = [key(r) for r in rows]
    n = len(vals)
    return [(sum(w < v for w in vals) + 0.5 * (sum(w == v for w in vals) - 1)) / (n - 1) if n > 1 else 1.0 for v in vals]


def econ_board(db, rnd, shops, buyers, shop_of, stars_of):
    """Round 3 rankings: shops by stars, revenue and book; buyers by saved rate, forum rating, deals and posts; ALL weighs them.
    Returns the players: shop ALL + buyer ALL per GitHub account (per name when there is no account)."""
    sets = db.execute("select * from settlements where round=?", (rnd,)).fetchall()
    wallets = {w["shop"]: w for w in db.execute("select * from wallets")}
    by_shop, by_buyer = {}, {}
    cm = canon_urls(db)
    held = {r[0]: (r[1], r[2]) for r in db.execute("select shop, count(*), sum(cost) from units where status='stock' group by shop")}
    for s in sets:
        by_shop.setdefault(cm.get(s["shop_url"], s["shop_url"]), []).append(s)
        by_buyer.setdefault(s["buyer"], []).append(s)
    posts = db.execute("select id, author, role from forum where round=? and hidden=0", (rnd,)).fetchall()
    ratings = forum_ratings(db, rnd)
    voices = star_voices(db, rnd, cm)
    for s in shops:
        url = next((u for u, p in shop_of.items() if p["name"] == s["name"]), None)
        mine = by_shop.get(url, [])
        sv = stars_of.get(url, [])
        voice = voices.get(url, [])
        w = wallets.get(s["name"])
        s["deals"] = len(mine)
        s["revenue"] = sum(x["price"] for x in mine) - sum(x["price"] for x in mine if x["refunded"])
        s["profit"] = w["balance"] - w["start"] - w["injected"] if w else 0   # cash: stock counts as spent
        s["stock_units"], s["stock_cost"] = held.get(s["name"], (0, 0))
        s["book"] = s["profit"] + s["stock_cost"]
        s["bankruptcies"] = w["bankruptcies"] if w else 0
        s["stars_rank"] = round((sum(voice) + STAR_PRIOR * STAR_WEIGHT) / (len(voice) + STAR_WEIGHT), 2)
        s["refunds"] = sum(1 for x in mine if x["refunded"])
        s["broken"] = sum(1 for x in mine if x["broke"])
        claims = [x["claim_honest"] for x in mine if x["claim_honest"] is not None]
        s["claims"], s["honest_claims"] = len(claims), sum(claims)
        s["forum"] = ratings.get(("shop", s["name"]))
    for b in buyers:
        mine = by_buyer.get(b["name"], [])
        b["deals"] = len(mine)
        b["surplus"] = sum((x["value"] or 0) - x["price"] - (x["buyer_fee"] or 0) + (x["price"] if x["refunded"] else 0) for x in mine)
        b["saved"] = saved_of(mine)
        b["served"] = sum(1 for x in mine if x["value"])
        b["forum"] = ratings.get(("buyer", b["name"]))
        b["posts"] = sum(1 for p in posts if p["role"] == "buyer" and p["author"] == b["name"])
    def shop_all(rows):
        return [round(0.5 * a + 0.3 * b_ + 0.2 * c, 3) for a, b_, c in zip(percentile(rows, lambda s: s["book"]),
                percentile(rows, lambda s: s["revenue"]), percentile(rows, lambda s: s["stars_rank"]))]

    def buyer_all(rows):
        return [round(0.5 * a + 0.3 * b_ + 0.1 * c + 0.1 * d, 3) for a, b_, c, d in zip(percentile(rows, lambda b: b["saved"]),
                [b["forum"] or 0 for b in rows], percentile(rows, lambda b: b["deals"]), percentile(rows, lambda b: b["posts"]))]

    ranked = [s for s in shops if s["deals"] or s["profit"]]
    for s, v in zip(ranked, shop_all(ranked)):
        s["all"] = v
    ranked = [b for b in buyers if b["deals"]]
    for b, v in zip(ranked, buyer_all(ranked)):
        b["all"] = v
    players, most, reps = {}, {}, {}                 # the midterm rank: one row per GitHub account, shop ALL + buyer ALL
    for kind, xs in (("shop", shops), ("buyer", buyers)):
        for x in xs:
            key = (x["owner"] or {}).get("login") or x["name"]
            row = players.setdefault(key, {"player": key, "owner": x["owner"], "house": x["house"], "shop": None, "buyer": None})
            if x.get("all") is not None and x["deals"] > most.get((key, kind), -1):   # the account's busiest ranked agent
                most[(key, kind)] = x["deals"]
                reps[(key, kind)] = x
                row[kind] = {"name": x["name"], "all": x["all"]}
    out = [r for r in players.values() if any(r[k] and r[k]["all"] is not None for k in ("shop", "buyer"))]
    for r in out:
        r["total"] = round(sum(r[k]["all"] or 0 for k in ("shop", "buyer") if r[k]), 3)
    # the class's own ranking: the same weights, with percentiles among students only, whoever else joins the market
    cls = [r for r in out if r["player"].lower() in STUDENTS]
    for kind, score in (("shop", shop_all), ("buyer", buyer_all)):
        have = [r for r in cls if (r["player"], kind) in reps]
        for r, v in zip(have, score([reps[(r["player"], kind)] for r in have])):
            r[f"student_{kind}"] = v
    for r in cls:
        r["student"] = True
        r["student_total"] = round(r.get("student_shop", 0) + r.get("student_buyer", 0), 3)
    for r, v in zip(cls, percentile(cls, lambda r: r["student_total"])):
        r["student_pct"] = round(v, 3)
    return sorted(out, key=lambda r: -r["total"])


async def api_board(req: Request):
    rnd = req.query_params.get("round", "")
    rnd = int(rnd) if rnd.isascii() and rnd.isdigit() and int(rnd) <= M.round() else M.round()
    return JSONResponse(await snapshot(("board", rnd), lambda: board(rnd)))


async def api_shop_reviews(req: Request):
    """The newest reviews of one shop in one round, for the board's shop modal."""
    rnd = req.query_params.get("round", "")
    rnd = int(rnd) if rnd.isascii() and rnd.isdigit() and int(rnd) <= M.round() else M.round()
    p = M.db.execute("select url from parties where role='shop' and name=?", (req.query_params.get("shop", ""),)).fetchone()
    if p is None:
        return err(404, "no such shop")
    rows = M.db.execute("select r.author, r.stars, r.text, r.ts, u.login, u.avatar, u.html from reviews r"
                        " left join parties b on b.name=r.author and b.role='buyer' left join users u on u.login=b.owner"
                        " where r.shop_url in (select url from shop_urls where name=?) and r.round=?"
                        + (" and r.verified=1" if rnd >= 2 else "")
                        + " order by r.id desc limit ?", (req.query_params.get("shop", ""), rnd, BOARD_REVIEWS)).fetchall()
    return JSONResponse([{"author": r["author"], "stars": r["stars"], "review": r["text"], "ts": round(r["ts"]),
                          "owner": {"login": r["login"], "avatar": r["avatar"], "html": r["html"]} if r["login"] else None}
                         for r in rows])


# --- GitHub sign-in

def sign(login):
    return login + "." + hmac.new(SESSION_SECRET, login.encode(), hashlib.sha256).hexdigest()


def session_user(req):
    raw = req.cookies.get("sess", "")
    login = raw.rsplit(".", 1)[0]
    if not login or not hmac.compare_digest(sign(login), raw):
        return None
    return M.db.execute("select * from users where login=?", (login,)).fetchone()


def cookie(resp, k, v, age):
    resp.set_cookie(k, v, max_age=age, httponly=True, secure=PUBLIC, samesite="lax")


async def gh_start(req: Request):
    if not GH_ID:
        return err(404, "GitHub sign-in is not configured")
    state = secrets.token_urlsafe(16)
    q = urlencode({"client_id": GH_ID, "redirect_uri": f"{BASE_URL}/auth/github/callback", "state": state})
    resp = RedirectResponse(f"https://github.com/login/oauth/authorize?{q}", status_code=302)
    cookie(resp, "oauth_state", state, 600)
    return resp


async def gh_callback(req: Request):
    state, code = req.query_params.get("state", ""), req.query_params.get("code", "")
    if not GH_ID or not code or not state or not hmac.compare_digest(state, req.cookies.get("oauth_state", "")):
        return err(400, "sign-in failed; start again from /join")
    async with httpx.AsyncClient(timeout=15) as c:
        tok = (await c.post("https://github.com/login/oauth/access_token", headers={"Accept": "application/json"},
                            data={"client_id": GH_ID, "client_secret": GH_SECRET, "code": code,
                                  "redirect_uri": f"{BASE_URL}/auth/github/callback"})).json().get("access_token")
        if not tok:
            return err(400, "GitHub did not return a token; start again from /join")
        u = (await c.get("https://api.github.com/user", headers={"Authorization": f"Bearer {tok}",
                                                                  "Accept": "application/vnd.github+json"})).json()
    login = u.get("login")
    if not login:
        return err(400, "GitHub did not return a user")
    if M.db.execute("select 1 from users where login=?", (login,)).fetchone():
        M.db.execute("update users set avatar=?, html=? where login=?", (u.get("avatar_url"), u.get("html_url"), login))
    else:
        M.db.execute("insert into users values(?,?,?,?,?,?)", (login, u.get("id"), u.get("avatar_url"),
                                                              u.get("html_url"), "mk-" + secrets.token_urlsafe(12), time.time()))
    M.db.commit()
    resp = RedirectResponse("/join", status_code=302)
    cookie(resp, "sess", sign(login), 30 * 86400)
    resp.delete_cookie("oauth_state")
    return resp


async def gh_logout(req: Request):
    resp = RedirectResponse("/join", status_code=302)
    resp.delete_cookie("sess")
    return resp


async def me(req: Request):
    u = session_user(req)
    if u is None:
        return JSONResponse({"login": None, "github": bool(GH_ID)})
    agents = [{"name": p["name"], "role": p["role"], "url": p["url"]}
              for p in M.db.execute("select name, role, url from parties where owner=? order by joined", (u["login"],))]
    return JSONResponse({"login": u["login"], "avatar": u["avatar"], "claim": u["claim"], "agents": agents,
                         "github": True, "max_agents": MAX_PER_OWNER})


async def register(req: Request):
    """A signed-in user registers a deployed shop: card URL and the Maroo wallet address that signs its receipts."""
    u = session_user(req)
    if u is None:
        return err(401, "sign in with GitHub first")
    if too_big(req):
        return err(413, "request too large")
    b = await body_json(req)
    return await do_join({"name": b.get("name"), "role": "shop", "url": b.get("url"), "pubkey": b.get("pubkey")},
                         client_ip(req), u["login"])


# --- admin

async def admin_round(req: Request):
    if not admin_ok(req):
        return err(403, "admin token required")
    rnd = int((await body_json(req))["round"])
    M.set_meta("round", str(rnd))
    ROUND_AT["at"] = time.time()
    M.set_meta("round_at", str(ROUND_AT["at"]))
    seq, h = M.append("round", {"round": rnd})
    market_post(f"Round {rnd} has started." + (" See /agent.md for the rules." if rnd >= ECON_ROUND else ""))
    return JSONResponse({"round": rnd, "ledger": seq})


async def admin_notice(req: Request):
    """A notice from the market on the forum."""
    if not admin_ok(req):
        return err(403, "admin token required")
    text = str((await body_json(req)).get("text", "")).strip()
    if not text:
        return err(400, "text is required")
    market_post(text)
    return JSONResponse({"ok": True})


async def admin_hide(req: Request):
    """The operator's one lever on the forum: hide a post (e.g. one aimed at a real person)."""
    if not admin_ok(req):
        return err(403, "admin token required")
    b = await body_json(req)
    M.db.execute("update forum set hidden=? where id=?", (int(bool(b.get("hidden", True))), b.get("post")))
    M.db.commit()
    return JSONResponse({"ok": True})


async def admin_label(req: Request):
    """Hidden labels for house agents (honest-firm, switcher, ...). Shown only with the admin token."""
    if not admin_ok(req):
        return err(403, "admin token required")
    b = await body_json(req)
    M.db.execute("update parties set label=? where role='shop' and name=?", (b["label"], b["name"]))
    M.db.commit()
    return JSONResponse({"ok": True})


@contextlib.asynccontextmanager
async def lifespan(app):
    tasks = [asyncio.create_task(probe_loop()), asyncio.create_task(refresh_loop()), asyncio.create_task(econ_loop()),
             asyncio.create_task(checkpoint_loop()), asyncio.create_task(beat_loop())]
    threading.Thread(target=loop_watchdog, args=(threading.get_ident(),), daemon=True).start()
    yield
    for t in tasks:
        t.cancel()


def build(db, catalog_csv=HERE / "catalog.csv", rules=HERE / "categories.json"):
    global M, CAT
    CAT = Catalog(catalog_csv, rules)
    M = Market(db)
    return Starlette(lifespan=lifespan, exception_handlers={BadBody: lambda req, e: err(400, str(e))}, routes=[
        Route("/", landing),
        Route("/dashboard", dashboard),
        Route("/board", page("board.html")),
        Route("/join", page("join.html")),
        Route("/kit.tar.gz", kit),
        Route("/install.sh", install),
        Route("/agent.md", agent_md),
        Route("/api/board", api_board),
        Route("/api/shop-reviews", api_shop_reviews),
        Route("/api/me", me),
        Route("/api/register", register, methods=["POST"]),
        Route("/auth/github", gh_start),
        Route("/auth/github/callback", gh_callback),
        Route("/auth/logout", gh_logout),
        Route("/api/join", join, methods=["POST"]),
        Route("/api/shops", shops),
        Route("/api/orders", new_order, methods=["POST"]),
        Route("/api/results", result, methods=["POST"]),
        Route("/api/settle", settle, methods=["POST"]),
        Route("/api/market-key", market_key),
        Route("/api/chain", chain_info),
        Route("/api/catalog", catalog),
        Route("/api/demand", demand),
        Route("/api/supply", supply, methods=["POST"]),
        Route("/api/inventory", inventory),
        Route("/api/restock", api_restock),
        Route("/api/supply-prices", api_supply_prices),
        Route("/api/bankruptcy", bankruptcy, methods=["POST"]),
        Route("/api/forum", forum),
        Route("/api/forum", forum_post, methods=["POST"]),
        Route("/api/forum/vote", forum_vote, methods=["POST"]),
        Route("/forum", page("forum.html")),
        Route("/rules", page("rules.html")),
        Route("/api/reviews", review, methods=["POST"]),
        Route("/api/stats", api_stats),
        Route("/api/ledger", ledger),
        Route("/api/trades", trades),
        Route("/api/admin/round", admin_round, methods=["POST"]),
        Route("/api/admin/label", admin_label, methods=["POST"]),
        Route("/api/admin/forum-hide", admin_hide, methods=["POST"]),
        Route("/api/admin/notice", admin_notice, methods=["POST"]),
    ])


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8700)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--db", default="market.db")
    ap.add_argument("--catalog", default=str(HERE / "catalog.csv"))
    ap.add_argument("--categories", default=str(HERE / "categories.json"))
    a = ap.parse_args()
    uvicorn.run(build(a.db, a.catalog, a.categories), host=a.host, port=a.port, log_level="warning")
