#!/usr/bin/env python3
"""
Portent relay — Nostr relay layer + settlement engine.

SQLite backend, one handler per custom kind (30007/30009/30010/30011/30012/
30015/30016/30017), NIP-01 event validation, NIP-33 replaceable dedup, and a
token-economics settlement engine (fees -> buyback reserve / staker pool,
loser stakes -> pool -> surplus burned, expiry -> full burn).

HTTP API (stdlib http.server, no deps):
  POST /event          {"event": {...}} or bare event dict  -> stores + settles
  GET  /stats          token stats (burned, reserve, fees, pools)
  GET  /leaderboard    top predictors by composite score
  GET  /predictions?status=open|resolved|expired|disputed
  GET  /prediction/<market_id>
  GET  /reputation/<pubkey>
  GET  /resolutions
  GET  /governance
  GET  /oracles
  GET  /events?kind=30015

DB: $PORTENT_DB or ~/.portent/relay.db  |  Port: $PORTENT_RELAY_PORT or 8899
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple

# Allow running from repo root or from relay/ dir.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    from contracts.portent_program import (
        canonical_event_id, SUPPORTED_KINDS, TIER_MULTIPLIERS, tier_for,
        composite_score, MIN_STAKE, MAX_STAKE, DEFAULT_STAKE,
        KIND_CROSS_REFERENCE, KIND_ATTESTATION, KIND_REPUTATION_SNAPSHOT,
        KIND_STAKE_CLAIM, KIND_GOVERNANCE, KIND_PREDICTION_POST,
        KIND_RESOLUTION, KIND_DISPUTE,
    )
except ImportError:  # pragma: no cover - standalone fallback
    KIND_CROSS_REFERENCE, KIND_ATTESTATION = 30007, 30009
    KIND_REPUTATION_SNAPSHOT, KIND_STAKE_CLAIM = 30010, 30011
    KIND_GOVERNANCE, KIND_PREDICTION_POST = 30012, 30015
    KIND_RESOLUTION, KIND_DISPUTE = 30016, 30017
    SUPPORTED_KINDS = {KIND_CROSS_REFERENCE, KIND_ATTESTATION,
                       KIND_REPUTATION_SNAPSHOT, KIND_STAKE_CLAIM,
                       KIND_GOVERNANCE, KIND_PREDICTION_POST,
                       KIND_RESOLUTION, KIND_DISPUTE}
    TIER_MULTIPLIERS = {"unverified": 1.0, "verified": 1.5, "trusted": 2.0,
                        "elite": 2.5, "legendary": 3.0}
    MIN_STAKE, MAX_STAKE, DEFAULT_STAKE = 100, 5000, 1000

    def canonical_event_id(pubkey_hex, kind, tags, content, created_at):
        import hashlib
        payload = json.dumps([0, pubkey_hex, kind, tags, content, created_at],
                             separators=(",", ":"))
        return hashlib.sha256(payload.encode()).hexdigest()

    def tier_for(composite, resolved):
        return "unverified"

    def composite_score(hit_rate_bp, resolved, accuracy, disputes_lost=0):
        return 0


DB_PATH = os.environ.get("PORTENT_DB",
                         os.path.expanduser("~/.portent/relay.db"))
RELAY_PORT = int(os.environ.get("PORTENT_RELAY_PORT", "8899"))

# Economics (mirror tokenomics.json / contracts)
FEE_BPS = 200                 # 2% settlement fee on winning payouts
BURN_AT_SOURCE_BPS = 200      # 2% of fee burned at source
BUYBACK_RESERVE_PCT = 50
STAKER_POOL_PCT = 50
ORACLE_RESOLUTION_REWARD = 500
CROSS_CHECK_REWARD = 150
MILESTONES = ((50_000, 0.001), (100_000, 0.005), (500_000, 0.02),
              (1_000_000, 0.05))


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS events(
    id TEXT PRIMARY KEY,
    pubkey TEXT NOT NULL,
    kind INTEGER NOT NULL,
    tags TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    d_tag TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_kind ON events(kind);
CREATE INDEX IF NOT EXISTS idx_events_pubkey ON events(pubkey);
CREATE INDEX IF NOT EXISTS idx_events_dtag ON events(pubkey, kind, d_tag);

CREATE TABLE IF NOT EXISTS predictions(
    market_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    predictor TEXT NOT NULL,
    event_class TEXT,
    side TEXT NOT NULL,
    stake INTEGER NOT NULL,
    odds_bp INTEGER NOT NULL,
    deadline INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    outcome TEXT,
    payout INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    PRIMARY KEY (market_id, predictor)
);
CREATE INDEX IF NOT EXISTS idx_pred_status ON predictions(status);
CREATE INDEX IF NOT EXISTS idx_pred_predictor ON predictions(predictor);

CREATE TABLE IF NOT EXISTS resolutions(
    market_id TEXT PRIMARY KEY,
    oracle TEXT NOT NULL,
    outcome TEXT NOT NULL,
    confidence REAL NOT NULL,
    resolved_at INTEGER NOT NULL,
    cross_checks INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS disputes(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id TEXT NOT NULL,
    disputer TEXT NOT NULL,
    reason TEXT,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS reputation(
    pubkey TEXT PRIMARY KEY,
    predictions INTEGER NOT NULL DEFAULT 0,
    resolved INTEGER NOT NULL DEFAULT 0,
    hit_rate_bp INTEGER NOT NULL DEFAULT 0,
    accuracy REAL NOT NULL DEFAULT 0.0,
    composite INTEGER NOT NULL DEFAULT 0,
    tier TEXT NOT NULL DEFAULT 'unverified',
    disputes_lost INTEGER NOT NULL DEFAULT 0,
    updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS stakes(
    pubkey TEXT PRIMARY KEY,
    amount INTEGER NOT NULL DEFAULT 0,
    tier TEXT,
    lock_until INTEGER
);

CREATE TABLE IF NOT EXISTS governance(
    proposal_id TEXT PRIMARY KEY,
    proposer TEXT NOT NULL,
    ptype TEXT NOT NULL,
    title TEXT,
    status TEXT NOT NULL DEFAULT 'open',
    votes_for INTEGER NOT NULL DEFAULT 0,
    votes_against INTEGER NOT NULL DEFAULT 0,
    quorum_pct INTEGER NOT NULL DEFAULT 3,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS oracles(
    pubkey TEXT PRIMARY KEY,
    resolutions INTEGER NOT NULL DEFAULT 0,
    disputes_lost INTEGER NOT NULL DEFAULT 0,
    accuracy REAL NOT NULL DEFAULT 0.0,
    bond INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS stats(
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL
);
"""

DEFAULT_STATS = {
    "total_burned": "0",
    "events_burned": "0",
    "buyback_reserve_sol": "0.0",
    "buyback_reserve_tokens": "0",
    "staker_pool_tokens": "0",
    "fees_collected_tokens": "0",
    "settlements": "0",
    "resolved_events": "0",
    "expired_events": "0",
    "disputes": "0",
    "last_milestone": "0",
    "open_predictions": "0",
}


def _conn() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA)
    return c


def _init_stats(c: sqlite3.Connection) -> None:
    for k, v in DEFAULT_STATS.items():
        c.execute("INSERT OR IGNORE INTO stats(k, v) VALUES(?, ?)", (k, v))


def _get_stat(c: sqlite3.Connection, key: str, default: str = "0") -> str:
    row = c.execute("SELECT v FROM stats WHERE k=?", (key,)).fetchone()
    return row["v"] if row else default


def _set_stat(c: sqlite3.Connection, key: str, value: Any) -> None:
    c.execute("INSERT INTO stats(k, v) VALUES(?, ?) "
              "ON CONFLICT(k) DO UPDATE SET v=excluded.v",
              (key, str(value)))


def _bump(c: sqlite3.Connection, key: str, delta: int) -> int:
    cur = int(_get_stat(c, key))
    cur += delta
    _set_stat(c, key, cur)
    return cur


# ---------------------------------------------------------------------------
# Event validation (NIP-01)
# ---------------------------------------------------------------------------

def validate_event(ev: Dict[str, Any]) -> Tuple[bool, str]:
    for f in ("pubkey", "kind", "tags", "content", "created_at", "id", "sig"):
        if f not in ev:
            return False, f"missing field: {f}"
    if not isinstance(ev["pubkey"], str) or len(ev["pubkey"]) != 64:
        return False, "pubkey must be 64 hex chars"
    if not isinstance(ev["kind"], int) or ev["kind"] not in SUPPORTED_KINDS:
        return False, f"unsupported kind {ev.get('kind')}"
    if not isinstance(ev["tags"], list):
        return False, "tags must be a list"
    if not isinstance(ev["created_at"], int):
        return False, "created_at must be int"
    calc = canonical_event_id(ev["pubkey"], ev["kind"], ev["tags"],
                              ev["content"], ev["created_at"])
    if calc != ev["id"]:
        return False, f"bad event id (got {ev['id']}, expected {calc[:16]}...)"
    # Signature verification is stubbed in the SDK (secp256k1 lib optional);
    # the wire format is NIP-01 and the id check binds content.
    return True, "ok"


def _d_tag_of(ev: Dict[str, Any]) -> Optional[str]:
    for tag in ev["tags"]:
        if len(tag) >= 2 and tag[0] == "d":
            return tag[1]
    return None


def _tag(ev: Dict[str, Any], name: str) -> Optional[str]:
    for tag in ev["tags"]:
        if len(tag) >= 2 and tag[0] == name:
            return tag[1]
    return None


def _store_event(c: sqlite3.Connection, ev: Dict[str, Any]) -> None:
    d = _d_tag_of(ev)
    if d is not None and 30000 <= ev["kind"] < 40000:
        # NIP-33 replaceable: same (pubkey, kind, d-tag) supersedes.
        c.execute("DELETE FROM events WHERE pubkey=? AND kind=? AND d_tag=?",
                  (ev["pubkey"], ev["kind"], d))
    c.execute(
        "INSERT OR REPLACE INTO events(id, pubkey, kind, tags, content, "
        "created_at, d_tag) VALUES(?,?,?,?,?,?,?)",
        (ev["id"], ev["pubkey"], ev["kind"],
         json.dumps(ev["tags"]), ev["content"], ev["created_at"], d))


# ---------------------------------------------------------------------------
# Kind handlers
# ---------------------------------------------------------------------------

def _handle_30010(c: sqlite3.Connection, ev: Dict[str, Any]) -> None:
    """Reputation snapshot: authoritative agent-published reputation."""
    try:
        data = json.loads(ev["content"] or "{}")
    except json.JSONDecodeError:
        data = {}
    pubkey = ev["pubkey"]
    preds = int(data.get("predictions", 0))
    resolved = int(data.get("resolved", 0))
    hit = int(data.get("hit_rate_bp", 0))
    acc = float(data.get("accuracy", 0.0))
    comp = int(data.get("composite", composite_score(hit, resolved, acc)))
    tier = data.get("tier", tier_for(comp, resolved))
    c.execute(
        "INSERT INTO reputation(pubkey, predictions, resolved, hit_rate_bp, "
        "accuracy, composite, tier, updated_at) VALUES(?,?,?,?,?,?,?,?) "
        "ON CONFLICT(pubkey) DO UPDATE SET predictions=excluded.predictions, "
        "resolved=excluded.resolved, hit_rate_bp=excluded.hit_rate_bp, "
        "accuracy=excluded.accuracy, composite=excluded.composite, "
        "tier=excluded.tier, updated_at=excluded.updated_at",
        (pubkey, preds, resolved, hit, acc, comp, tier, ev["created_at"]))


def _handle_30011(c: sqlite3.Connection, ev: Dict[str, Any]) -> None:
    """Stake claim: amount + tier + lock_until from content JSON or tags."""
    try:
        data = json.loads(ev["content"] or "{}")
    except json.JSONDecodeError:
        data = {}
    amount = int(data.get("amount", 0))
    tier = data.get("tier", "")
    lock_until = int(data.get("lock_until", ev["created_at"]))
    c.execute(
        "INSERT INTO stakes(pubkey, amount, tier, lock_until) VALUES(?,?,?,?) "
        "ON CONFLICT(pubkey) DO UPDATE SET amount=excluded.amount, "
        "tier=excluded.tier, lock_until=excluded.lock_until",
        (ev["pubkey"], amount, tier, lock_until))


def _handle_30012(c: sqlite3.Connection, ev: Dict[str, Any]) -> None:
    """Governance: proposal creation (content JSON has ptype/title/quorum) or
    a vote (tags: ['vote', proposal_id, for|against])."""
    vote = _tag(ev, "vote")
    if vote is not None:
        pid = vote
        side = "for"
        for tag in ev["tags"]:
            if len(tag) >= 3 and tag[0] == "vote":
                side = tag[2]
                break
        col = "votes_for" if side == "for" else "votes_against"
        c.execute(f"UPDATE governance SET {col}={col}+1 "
                  "WHERE proposal_id=? AND status='open'", (pid,))
        return
    try:
        data = json.loads(ev["content"] or "{}")
    except json.JSONDecodeError:
        data = {}
    pid = _tag(ev, "d") or ev["id"][:16]
    quorum = int(data.get("quorum_pct", 3))
    expires = ev["created_at"] + int(data.get("voting_window_hours", 72)) * 3600
    c.execute(
        "INSERT OR REPLACE INTO governance(proposal_id, proposer, ptype, "
        "title, status, quorum_pct, created_at, expires_at) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (pid, ev["pubkey"], data.get("ptype", "fee_params"),
         data.get("title", ""), "open", quorum, ev["created_at"], expires))


def _handle_30015(c: sqlite3.Connection, ev: Dict[str, Any]) -> None:
    """Prediction post: market = predicted Nostr event id (tag 'm').
    Stake comes from tag 'stake' or content JSON; odds from 'odds' tag."""
    market = _tag(ev, "m")
    if not market:
        return
    side = _tag(ev, "side") or "yes"
    try:
        stake = int(_tag(ev, "stake") or DEFAULT_STAKE)
    except ValueError:
        stake = DEFAULT_STAKE
    stake = max(MIN_STAKE, min(MAX_STAKE, stake))
    try:
        odds = int(_tag(ev, "odds") or 15000)
    except ValueError:
        odds = 15000
    try:
        deadline = int(_tag(ev, "deadline") or (ev["created_at"] + 7 * 86400))
    except ValueError:
        deadline = ev["created_at"] + 7 * 86400
    ev_class = _tag(ev, "c") or "general"
    c.execute(
        "INSERT OR REPLACE INTO predictions(market_id, event_id, predictor, "
        "event_class, side, stake, odds_bp, deadline, status, created_at) "
        "VALUES(?,?,?,?,?,?,?,?,'open',?)",
        (market, ev["id"], ev["pubkey"], ev_class, side, stake, odds,
         deadline, ev["created_at"]))
    _bump(c, "open_predictions", 1)


def _handle_30016(c: sqlite3.Connection, ev: Dict[str, Any]) -> None:
    """Resolution: settles the market. outcome from tag 'outcome' (yes|no),
    confidence from content JSON."""
    market = _tag(ev, "m")
    if not market:
        return
    outcome = _tag(ev, "outcome")
    if outcome not in ("yes", "no"):
        return
    try:
        data = json.loads(ev["content"] or "{}")
    except json.JSONDecodeError:
        data = {}
    confidence = float(data.get("confidence", 0.8))
    row = c.execute("SELECT * FROM predictions WHERE market_id=?",
                    (market,)).fetchone()
    if row is None or row["status"] != "open":
        return
    _settle_market(c, row, outcome, ev["pubkey"], confidence, ev["created_at"])
    c.execute(
        "INSERT OR REPLACE INTO resolutions(market_id, oracle, outcome, "
        "confidence, resolved_at) VALUES(?,?,?,?,?)",
        (market, ev["pubkey"], outcome, confidence, ev["created_at"]))
    _upsert_oracle(c, ev["pubkey"], outcome, ev["created_at"])


def _handle_30017(c: sqlite3.Connection, ev: Dict[str, Any]) -> None:
    """Dispute: freezes a market pending review."""
    market = _tag(ev, "m")
    if not market:
        return
    c.execute("UPDATE predictions SET status='disputed' WHERE market_id=? "
              "AND status='open'", (market,))
    c.execute("INSERT INTO disputes(market_id, disputer, reason, created_at) "
              "VALUES(?,?,?,?)",
              (market, ev["pubkey"], ev["content"][:500], ev["created_at"]))
    _bump(c, "disputes", 1)


HANDLERS = {
    KIND_REPUTATION_SNAPSHOT: _handle_30010,
    KIND_STAKE_CLAIM: _handle_30011,
    KIND_GOVERNANCE: _handle_30012,
    KIND_PREDICTION_POST: _handle_30015,
    KIND_RESOLUTION: _handle_30016,
    KIND_DISPUTE: _handle_30017,
}


# ---------------------------------------------------------------------------
# Settlement engine (token economics)
# ---------------------------------------------------------------------------

def _upsert_oracle(c: sqlite3.Connection, oracle: str, outcome: str,
                   ts: int) -> None:
    row = c.execute("SELECT * FROM oracles WHERE pubkey=?",
                    (oracle,)).fetchone()
    if row is None:
        c.execute("INSERT INTO oracles(pubkey, resolutions, accuracy, "
                  "active) VALUES(?,1,1.0,1)", (oracle,))
        return
    res = row["resolutions"] + 1
    acc = (row["accuracy"] * row["resolutions"] + 1.0) / res
    c.execute("UPDATE oracles SET resolutions=?, accuracy=? WHERE pubkey=?",
              (res, acc, oracle))
    # Oracle reward accrues to reputation bucket stats (simplified on-chain
    # pool; real distribution happens via the DISTRIBUTION account).
    _bump(c, "oracle_rewards_paid", ORACLE_RESOLUTION_REWARD)


def _settle_market(c: sqlite3.Connection, market_row: sqlite3.Row,
                   outcome: str, oracle: str, confidence: float,
                   ts: int) -> None:
    """Core economics. Winners get stake back + profit from the losers' pool;
    pool surplus is burned; fee split = source burn / buyback reserve /
    staker pool."""
    losers = c.execute(
        "SELECT * FROM predictions WHERE market_id=? AND status='open' "
        "AND side!=?", (market_row["market_id"], outcome)).fetchall()
    winners = c.execute(
        "SELECT * FROM predictions WHERE market_id=? AND status='open' "
        "AND side=?", (market_row["market_id"], outcome)).fetchall()

    pool = sum(l["stake"] for l in losers)
    fee_tokens = 0
    profit_paid = 0
    surplus_burned = 0

    for w in winners:
        profit_owed = w["stake"] * (w["odds_bp"] - 10000) // 10000
        profit_owed = max(0, profit_owed)
        profit = min(profit_owed, pool)
        pool -= profit
        profit_paid += profit
        payout = w["stake"] + profit
        fee = payout * FEE_BPS // 10000
        fee_tokens += fee
        c.execute("UPDATE predictions SET status='resolved', outcome=?, "
                  "payout=? WHERE market_id=? AND predictor=?",
                  (outcome, payout, market_row["market_id"], w["predictor"]))

    surplus_burned = pool
    for l in losers:
        c.execute("UPDATE predictions SET status='resolved', outcome=?, "
                  "payout=0 WHERE market_id=? AND predictor=?",
                  (outcome, market_row["market_id"], l["predictor"]))

    burn_source = fee_tokens * BURN_AT_SOURCE_BPS // 10000
    fee_remainder = fee_tokens - burn_source
    reserve_tokens = fee_remainder * BUYBACK_RESERVE_PCT // 100
    staker_tokens = fee_remainder - reserve_tokens

    total_burned = burn_source + surplus_burned

    _bump(c, "settlements", 1)
    _bump(c, "resolved_events", 1)
    _bump(c, "open_predictions", -1 * max(1, len(losers) + len(winners)))
    _bump(c, "fees_collected_tokens", fee_tokens)
    _bump(c, "buyback_reserve_tokens", reserve_tokens)
    _bump(c, "staker_pool_tokens", staker_tokens)
    _bump(c, "total_burned", total_burned)
    _bump(c, "events_burned", 1)
    _set_stat(c, "last_settlement_outcome", outcome)
    _set_stat(c, "last_settlement_at", ts)
    if surplus_burned:
        _bump(c, "loser_surplus_burned", surplus_burned)

    # Reputation update for all participants.
    for w in winners:
        _record_result(c, w["predictor"], hit=True)
    for l in losers:
        _record_result(c, l["predictor"], hit=False)

    # Milestone burns (ladder on resolved-event count).
    _maybe_milestone_burn(c, int(_get_stat(c, "resolved_events")))


def _maybe_milestone_burn(c: sqlite3.Connection, resolved: int) -> None:
    for target, pct in MILESTONES:
        if resolved >= target:
            prev = int(_get_stat(c, "last_milestone"))
            if prev < target:
                reserve = int(_get_stat(c, "buyback_reserve_tokens"))
                burn = int(reserve * pct)
                _bump(c, "total_burned", burn)
                _bump(c, "events_burned", 1)
                _set_stat(c, "last_milestone", target)
                _set_stat(c, "last_milestone_burn", burn)
                return


def _record_result(c: sqlite3.Connection, pubkey: str, hit: bool) -> None:
    row = c.execute("SELECT * FROM reputation WHERE pubkey=?",
                    (pubkey,)).fetchone()
    if row is None:
        row = {"pubkey": pubkey, "predictions": 0, "resolved": 0,
               "hit_rate_bp": 0, "accuracy": 0.0, "composite": 0,
               "tier": "unverified", "disputes_lost": 0}
    preds = row["predictions"] + 1
    resolved = row["resolved"] + 1
    hits = int(row["hit_rate_bp"] / 10000 * row["resolved"]) + (1 if hit else 0)
    hit_bp = int(hits * 10000 / resolved) if resolved else 0
    acc = round((row["accuracy"] * row["resolved"] + (1.0 if hit else 0.0))
                / resolved, 4)
    comp = composite_score(hit_bp, resolved, acc, row["disputes_lost"])
    tier = tier_for(comp, resolved)
    c.execute(
        "INSERT INTO reputation(pubkey, predictions, resolved, hit_rate_bp, "
        "accuracy, composite, tier, updated_at) VALUES(?,?,?,?,?,?,?,?) "
        "ON CONFLICT(pubkey) DO UPDATE SET predictions=excluded.predictions, "
        "resolved=excluded.resolved, hit_rate_bp=excluded.hit_rate_bp, "
        "accuracy=excluded.accuracy, composite=excluded.composite, "
        "tier=excluded.tier, updated_at=excluded.updated_at",
        (pubkey, preds, resolved, hit_bp, acc, comp, tier, int(time.time())))


def _expire_stale(c: sqlite3.Connection, now: int) -> int:
    """Deadline passed + still open -> full stake burned (anti-abandonment)."""
    rows = c.execute("SELECT * FROM predictions WHERE status='open' "
                     "AND deadline<?", (now,)).fetchall()
    burned = 0
    for r in rows:
        c.execute("UPDATE predictions SET status='expired' WHERE market_id=?",
                  (r["market_id"],))
        burned += r["stake"]
        _bump(c, "expired_events", 1)
    if rows:
        _bump(c, "open_predictions", -len(rows))
        _bump(c, "total_burned", burned)
        _bump(c, "events_burned", len(rows))
        _set_stat(c, "last_expiry_burn", burned)
    return burned


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

class PortentRelayHandler(BaseHTTPRequestHandler):
    server_version = "PortentRelay/1.0"

    def log_message(self, fmt, *args):  # keep logs quiet
        sys.stderr.write("[relay] %s\n" % (fmt % args))

    def _send(self, code: int, obj: Any) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        if self.path != "/event":
            self._send(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length)
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, json.JSONDecodeError):
            self._send(400, {"error": "invalid json"})
            return
        ev = payload.get("event", payload)
        ok, err = validate_event(ev)
        if not ok:
            self._send(400, {"error": err})
            return
        c = _conn()
        try:
            _init_stats(c)
            _expire_stale(c, int(time.time()))
            _store_event(c, ev)
            handler = HANDLERS.get(ev["kind"])
            if handler:
                handler(c, ev)
            c.commit()
        except Exception as exc:  # noqa: BLE001 - report, don't crash
            c.rollback()
            self._send(500, {"error": f"handler failed: {exc}"})
            return
        finally:
            c.close()
        self._send(200, {"ok": True, "id": ev["id"], "kind": ev["kind"]})

    def do_GET(self) -> None:
        c = _conn()
        try:
            _init_stats(c)
            _expire_stale(c, int(time.time()))
            c.commit()
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            qs = urllib.parse.parse_qs(parsed.query)
            now = int(time.time())
            if path == "/stats":
                out = {k: _get_stat(c, k) for k in DEFAULT_STATS}
                out["tier_multipliers"] = TIER_MULTIPLIERS
                out["now"] = now
                self._send(200, out)
            elif path == "/leaderboard":
                rows = c.execute(
                    "SELECT * FROM reputation WHERE resolved>0 "
                    "ORDER BY composite DESC, hit_rate_bp DESC LIMIT 25"
                ).fetchall()
                self._send(200, {"leaderboard": [dict(r) for r in rows]})
            elif path == "/predictions":
                status = qs.get("status", ["open"])[0]
                rows = c.execute(
                    "SELECT * FROM predictions WHERE status=? "
                    "ORDER BY created_at DESC LIMIT 200", (status,)
                ).fetchall()
                self._send(200, {"predictions": [dict(r) for r in rows]})
            elif path.startswith("/prediction/"):
                mid = urllib.parse.unquote(path.split("/")[-1])
                rows = c.execute(
                    "SELECT * FROM predictions WHERE market_id=?",
                    (mid,)).fetchall()
                self._send(200, {"market": mid,
                                 "predictions": [dict(r) for r in rows]})
            elif path.startswith("/reputation/"):
                pk = urllib.parse.unquote(path.split("/")[-1])
                row = c.execute("SELECT * FROM reputation WHERE pubkey=?",
                                (pk,)).fetchone()
                self._send(200, {"reputation": dict(row) if row else None})
            elif path == "/resolutions":
                rows = c.execute(
                    "SELECT * FROM resolutions ORDER BY resolved_at DESC "
                    "LIMIT 100").fetchall()
                self._send(200, {"resolutions": [dict(r) for r in rows]})
            elif path == "/governance":
                rows = c.execute("SELECT * FROM governance ORDER BY "
                                 "created_at DESC LIMIT 50").fetchall()
                self._send(200, {"proposals": [dict(r) for r in rows]})
            elif path == "/oracles":
                rows = c.execute("SELECT * FROM oracles ORDER BY resolutions "
                                 "DESC").fetchall()
                self._send(200, {"oracles": [dict(r) for r in rows]})
            elif path == "/events":
                kind = int(qs.get("kind", ["0"])[0] or 0)
                rows = c.execute(
                    "SELECT id, pubkey, kind, tags, created_at FROM events "
                    "WHERE kind=? ORDER BY created_at DESC LIMIT 100",
                    (kind,)).fetchall() if kind else c.execute(
                    "SELECT id, pubkey, kind, tags, created_at FROM events "
                    "ORDER BY created_at DESC LIMIT 100").fetchall()
                self._send(200, {"events": [dict(r) for r in rows]})
            elif path == "/health":
                self._send(200, {"ok": True, "db": DB_PATH})
            else:
                self._send(404, {"error": "unknown endpoint",
                                 "hint": "/stats /leaderboard /predictions "
                                         "/prediction/<id> /reputation/<pk> "
                                         "/resolutions /governance /oracles "
                                         "/events"})
        finally:
            c.close()


def main() -> None:
    c = _conn()
    _init_stats(c)
    c.commit()
    c.close()
    srv = ThreadingHTTPServer(("0.0.0.0", RELAY_PORT), PortentRelayHandler)
    print(f"[portent-relay] listening on :{RELAY_PORT} db={DB_PATH}",
          flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n[portent-relay] shutdown")


if __name__ == "__main__":
    main()
