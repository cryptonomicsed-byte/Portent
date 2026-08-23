#!/usr/bin/env python3
"""
Portent on-chain data model — Solana account layouts, serialization,
Nostr<->Solana cross-reference builders, and pump.fun launch params.

Standalone module: `python3 contracts/portent_program.py` prints the account
manifest + launch params. No external dependencies (stdlib only).

All integers little-endian unless noted. Discriminator byte 0x01..0x08
prefixes each account so a single on-chain program can route deserialization.
"""
from __future__ import annotations

import json
import hashlib
import os
import struct
import sys
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Constants (mirror tokenomics.json; loadable, with hardcoded defaults so the
# module is runnable even without the JSON present).
# ---------------------------------------------------------------------------

TOTAL_SUPPLY = 1_000_000_000
DECIMALS = 9
SYMBOL = "PORT"
NAME = "Portent"

FEE_BPS = 200                      # 2% settlement fee on winning payouts
BURN_AT_SOURCE_BPS = 200           # 2% of the fee burned at source
BUYBACK_RESERVE_PCT = 50           # of fee remainder
STAKER_POOL_PCT = 50               # of fee remainder
MIN_STAKE = 100
MAX_STAKE = 5_000
DEFAULT_STAKE = 1_000
COOLDOWN_SECONDS = 60
DAILY_CAP = 50_000
MIN_BUYBACK_SOL = 0.1
MAX_BUYBACK_SOL = 5.0
AUTO_TRIGGER_RESERVE_SOL = 100.0
QUORUM_PCT = 3
VOTING_WINDOW_HOURS = 72
EXECUTION_DELAY_HOURS = 24
MIN_PROPOSAL_STAKE = 100_000

KIND_CROSS_REFERENCE = 30007
KIND_ATTESTATION = 30009
KIND_REPUTATION_SNAPSHOT = 30010
KIND_STAKE_CLAIM = 30011
KIND_GOVERNANCE = 30012
KIND_PREDICTION_POST = 30015
KIND_RESOLUTION = 30016
KIND_DISPUTE = 30017
SUPPORTED_KINDS = {
    KIND_CROSS_REFERENCE, KIND_ATTESTATION, KIND_REPUTATION_SNAPSHOT,
    KIND_STAKE_CLAIM, KIND_GOVERNANCE, KIND_PREDICTION_POST,
    KIND_RESOLUTION, KIND_DISPUTE,
}

# Odds are expressed in basis points where 10000 = even money (1.0x).
# A 2.0x market (double-or-nothing) is 20000bp. Profit on a win =
# stake * (odds_bp - 10000) / 10000.
ODDS_MENU_BP = (11000, 12000, 15000, 20000, 30000, 40000)

TIER_MULTIPLIERS = {
    "unverified": 1.0, "verified": 1.5, "trusted": 2.0, "elite": 2.5,
    "legendary": 3.0,
}
TIER_THRESHOLDS = (
    # (min_composite, min_resolved, name)
    (800, 50, "legendary"),
    (500, 30, "elite"),
    (300, 15, "trusted"),
    (100, 5, "verified"),
)

ACCOUNT_DISCRIMINATORS = {
    "PREDICTION": 0x01, "RESOLUTION": 0x02, "REPUTATION": 0x03,
    "DISTRIBUTION": 0x04, "BURN": 0x05, "STAKE": 0x06,
    "CONFIG": 0x07, "ORACLE": 0x08,
}

PUBKEY_BYTES = 32
HASH_BYTES = 32
LAMPORTS_PER_SOL = 1_000_000_000


def load_tokenomics(path: Optional[str] = None) -> Dict[str, Any]:
    """Load tokenomics.json if reachable; caller falls back to defaults."""
    if path is None:
        path = os.path.join(os.path.dirname(__file__), "..", "tokenomics.json")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}


# ---------------------------------------------------------------------------
# Canonical Nostr event id (NIP-01): sha256 of the JSON-serialized array
# [0, pubkey, kind, tags, content, created_at] with compact separators.
# ---------------------------------------------------------------------------

def canonical_event_id(pubkey_hex: str, kind: int, tags: List[List[str]],
                       content: str, created_at: int) -> str:
    payload = json.dumps([0, pubkey_hex, kind, tags, content, created_at],
                         separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def d_tag_for(kind: int, *parts: str) -> str:
    """Product-scoped deterministic d-tag (NIP-33) — never collides with
    PROOF's sha256(channel:event) tags on shared relays."""
    return "portent:" + ":".join(parts)


# ---------------------------------------------------------------------------
# Account data classes
# ---------------------------------------------------------------------------

@dataclass
class PredictionAccount:
    discriminator: int = ACCOUNT_DISCRIMINATORS["PREDICTION"]
    predictor: bytes = b""                 # 32B ed25519 pubkey
    market_id: str = ""                    # sha256 of the predicted event
    event_class: str = ""                  # e.g. "token", "protocol", "social"
    side: str = "yes"                      # "yes" | "no"
    stake: int = 0                         # raw units (9 dp)
    odds_bp: int = 0                       # odds in basis points (2000 = 2.0x)
    deadline: int = 0                      # unix ts
    status: str = "open"                   # open | resolved | expired | disputed
    outcome: str = ""                      # resolved outcome
    payout: int = 0                        # raw units paid out (0 if lost)
    created_at: int = 0

    def to_bytes(self) -> bytes:
        market = self.market_id.encode()[:32].ljust(32, b"\x00")
        cls = self.event_class.encode()[:16].ljust(16, b"\x00")
        side = self.side.encode()[:8].ljust(8, b"\x00")
        status = self.status.encode()[:8].ljust(8, b"\x00")
        outcome = self.outcome.encode()[:8].ljust(8, b"\x00")
        return struct.pack(
            "<B32s16s8s8sQHq8s8sQq",   # see parse() for layout
            self.discriminator, self.predictor, market, cls, side,
            self.stake, self.odds_bp, self.deadline, status, outcome,
            self.payout, self.created_at,
        )

    @classmethod
    def from_bytes(cls, raw: bytes) -> "PredictionAccount":
        vals = struct.unpack("<B32s16s8s8sQHq8s8sQq", raw)
        a = cls()
        (a.discriminator, a.predictor, market, cls, side, a.stake,
         a.odds_bp, a.deadline, status, outcome, a.payout,
         a.created_at) = vals
        a.market_id = market.rstrip(b"\x00").decode()
        a.event_class = cls.rstrip(b"\x00").decode()
        a.side = side.rstrip(b"\x00").decode()
        a.status = status.rstrip(b"\x00").decode()
        a.outcome = outcome.rstrip(b"\x00").decode()
        return a

    def size(self) -> int:
        return len(self.to_bytes())


@dataclass
class ResolutionAccount:
    discriminator: int = ACCOUNT_DISCRIMINATORS["RESOLUTION"]
    market_id: str = ""
    oracle: bytes = b""
    outcome: str = ""
    confidence: float = 0.0
    bond: int = 0
    resolved_at: int = 0
    cross_checks: int = 0

    def to_bytes(self) -> bytes:
        market = self.market_id.encode()[:32].ljust(32, b"\x00")
        outcome = self.outcome.encode()[:8].ljust(8, b"\x00")
        return struct.pack("<B32s32s8sfQqI", self.discriminator, market,
                           self.oracle, outcome, self.confidence, self.bond,
                           self.resolved_at, self.cross_checks)

    @classmethod
    def from_bytes(cls, raw: bytes) -> "ResolutionAccount":
        vals = struct.unpack("<B32s32s8sfQqI", raw)
        a = cls()
        (a.discriminator, market, a.oracle, outcome, a.confidence, a.bond,
         a.resolved_at, a.cross_checks) = vals
        a.market_id = market.rstrip(b"\x00").decode()
        a.outcome = outcome.rstrip(b"\x00").decode()
        return a

    def size(self) -> int:
        return len(self.to_bytes())


@dataclass
class ReputationAccount:
    discriminator: int = ACCOUNT_DISCRIMINATORS["REPUTATION"]
    pubkey: bytes = b""
    predictions: int = 0
    resolved: int = 0
    hit_rate_bp: int = 0                   # basis points of resolved won
    accuracy: float = 0.0
    composite: int = 0                     # 0..1000
    tier: str = "unverified"
    linked_communities: List[str] = field(default_factory=list)
    updated_at: int = 0

    def to_bytes(self) -> bytes:
        tier = self.tier.encode()[:12].ljust(12, b"\x00")
        comms = b"".join(c.encode()[:32].ljust(32, b"\x00")
                         for c in self.linked_communities[:4])
        comms = comms.ljust(4 * 32, b"\x00")
        return struct.pack("<B32sIIIfI12s128sq", self.discriminator,
                           self.pubkey, self.predictions, self.resolved,
                           self.hit_rate_bp, self.accuracy, self.composite,
                           tier, comms, self.updated_at)

    @classmethod
    def from_bytes(cls, raw: bytes) -> "ReputationAccount":
        vals = struct.unpack("<B32sIIIfI12s128sq", raw)
        a = cls()
        (a.discriminator, a.pubkey, a.predictions, a.resolved,
         a.hit_rate_bp, a.accuracy, a.composite, tier, comms,
         a.updated_at) = vals
        a.tier = tier.rstrip(b"\x00").decode()
        a.linked_communities = [
            comms[i * 32:(i + 1) * 32].rstrip(b"\x00").decode()
            for i in range(4) if comms[i * 32:(i + 1) * 32].rstrip(b"\x00")
        ]
        return a

    def size(self) -> int:
        return len(self.to_bytes())


@dataclass
class DistributionAccount:
    discriminator: int = ACCOUNT_DISCRIMINATORS["DISTRIBUTION"]
    predictor_oracle_rewards: int = 400_000_000 * 10 ** DECIMALS
    initial_liquidity: int = 100_000_000 * 10 ** DECIMALS
    team: int = 100_000_000 * 10 ** DECIMALS
    community_fund: int = 150_000_000 * 10 ** DECIMALS
    buyback_reserve: int = 100_000_000 * 10 ** DECIMALS
    burn_pool: int = 150_000_000 * 10 ** DECIMALS

    def to_bytes(self) -> bytes:
        return struct.pack("<B6Q", self.discriminator,
                           self.predictor_oracle_rewards,
                           self.initial_liquidity, self.team,
                           self.community_fund, self.buyback_reserve,
                           self.burn_pool)

    @classmethod
    def from_bytes(cls, raw: bytes) -> "DistributionAccount":
        vals = struct.unpack("<B6Q", raw)
        a = cls()
        (a.discriminator, a.predictor_oracle_rewards, a.initial_liquidity,
         a.team, a.community_fund, a.buyback_reserve, a.burn_pool) = vals
        return a

    def size(self) -> int:
        return len(self.to_bytes())

    def buckets(self) -> Dict[str, Dict[str, Any]]:
        labels = {
            "predictor_oracle_rewards": "Predictor + Oracle rewards (24mo)",
            "initial_liquidity": "Initial liquidity (12mo lock)",
            "team": "Team (3mo cliff + 18mo)",
            "community_fund": "Community fund (36mo, governance)",
            "buyback_reserve": "Buyback reserve",
            "burn_pool": "Burn pool",
        }
        out = {}
        for k, label in labels.items():
            raw = getattr(self, k)
            out[k] = {
                "label": label,
                "raw": raw,
                "tokens": raw / 10 ** DECIMALS,
                "pct": round(100 * raw / (TOTAL_SUPPLY * 10 ** DECIMALS), 2),
            }
        return out


@dataclass
class BurnAccount:
    discriminator: int = ACCOUNT_DISCRIMINATORS["BURN"]
    total_burned: int = 0
    events_burned: int = 0
    last_burn_at: int = 0
    last_stream: str = ""                  # settlement_fee|expired|loser|milestone|governance

    def to_bytes(self) -> bytes:
        stream = self.last_stream.encode()[:16].ljust(16, b"\x00")
        return struct.pack("<BQQq16s", self.discriminator, self.total_burned,
                           self.events_burned, self.last_burn_at, stream)

    @classmethod
    def from_bytes(cls, raw: bytes) -> "BurnAccount":
        vals = struct.unpack("<BQQq16s", raw)
        a = cls()
        (a.discriminator, a.total_burned, a.events_burned, a.last_burn_at,
         stream) = vals
        a.last_stream = stream.rstrip(b"\x00").decode()
        return a

    def size(self) -> int:
        return len(self.to_bytes())


@dataclass
class StakeAccount:
    discriminator: int = ACCOUNT_DISCRIMINATORS["STAKE"]
    pubkey: bytes = b""
    amount: int = 0
    tier: str = ""                         # 30d | 90d | 365d
    lock_until: int = 0
    reward_accum: int = 0
    last_compound: int = 0

    def to_bytes(self) -> bytes:
        tier = self.tier.encode()[:8].ljust(8, b"\x00")
        return struct.pack("<B32sQ8sqQq", self.discriminator, self.pubkey,
                           self.amount, tier, self.lock_until,
                           self.reward_accum, self.last_compound)

    @classmethod
    def from_bytes(cls, raw: bytes) -> "StakeAccount":
        vals = struct.unpack("<B32sQ8sqQq", raw)
        a = cls()
        (a.discriminator, a.pubkey, a.amount, tier, a.lock_until,
         a.reward_accum, a.last_compound) = vals
        a.tier = tier.rstrip(b"\x00").decode()
        return a

    def size(self) -> int:
        return len(self.to_bytes())


@dataclass
class ConfigAccount:
    discriminator: int = ACCOUNT_DISCRIMINATORS["CONFIG"]
    fee_bps: int = FEE_BPS
    burn_at_source_bps: int = BURN_AT_SOURCE_BPS
    buyback_reserve_pct: int = BUYBACK_RESERVE_PCT
    staker_pool_pct: int = STAKER_POOL_PCT
    min_stake: int = MIN_STAKE
    max_stake: int = MAX_STAKE
    default_stake: int = DEFAULT_STAKE
    cooldown_seconds: int = COOLDOWN_SECONDS
    daily_cap: int = DAILY_CAP
    min_buyback_lamports: int = int(MIN_BUYBACK_SOL * LAMPORTS_PER_SOL)
    max_buyback_lamports: int = int(MAX_BUYBACK_SOL * LAMPORTS_PER_SOL)
    auto_trigger_reserve_lamports: int = int(AUTO_TRIGGER_RESERVE_SOL * LAMPORTS_PER_SOL)
    oracle_panel: List[bytes] = field(default_factory=list)  # up to 8 pubkeys

    def to_bytes(self) -> bytes:
        panel = b"".join(p.ljust(32, b"\x00") for p in self.oracle_panel[:8])
        panel = panel.ljust(8 * 32, b"\x00")
        return struct.pack("<BHHHBBQQIIQQQ256s", self.discriminator,
                           self.fee_bps, self.burn_at_source_bps,
                           self.buyback_reserve_pct, self.staker_pool_pct,
                           self.min_stake, self.max_stake, self.default_stake,
                           self.cooldown_seconds, self.daily_cap,
                           self.min_buyback_lamports,
                           self.max_buyback_lamports,
                           self.auto_trigger_reserve_lamports, panel)

    @classmethod
    def from_bytes(cls, raw: bytes) -> "ConfigAccount":
        vals = struct.unpack("<BHHHBBQQIIQQQ256s", raw)
        a = cls()
        (a.discriminator, a.fee_bps, a.burn_at_source_bps,
         a.buyback_reserve_pct, a.staker_pool_pct, a.min_stake, a.max_stake,
         a.default_stake, a.cooldown_seconds, a.daily_cap,
         a.min_buyback_lamports, a.max_buyback_lamports,
         a.auto_trigger_reserve_lamports, panel) = vals
        a.oracle_panel = [panel[i * 32:(i + 1) * 32].rstrip(b"\x00")
                          for i in range(8)
                          if panel[i * 32:(i + 1) * 32].rstrip(b"\x00")]
        return a

    def size(self) -> int:
        return len(self.to_bytes())


@dataclass
class OracleAccount:
    discriminator: int = ACCOUNT_DISCRIMINATORS["ORACLE"]
    pubkey: bytes = b""
    resolutions: int = 0
    disputes_lost: int = 0
    accuracy: float = 0.0
    bond: int = 0
    active: bool = True

    def to_bytes(self) -> bytes:
        return struct.pack("<B32sIIfQ?", self.discriminator, self.pubkey,
                           self.resolutions, self.disputes_lost, self.accuracy,
                           self.bond, self.active)

    @classmethod
    def from_bytes(cls, raw: bytes) -> "OracleAccount":
        vals = struct.unpack("<B32sIIfQ?", raw)
        a = cls()
        (a.discriminator, a.pubkey, a.resolutions, a.disputes_lost,
         a.accuracy, a.bond, a.active) = vals
        return a

    def size(self) -> int:
        return len(self.to_bytes())


ACCOUNT_CLASSES = {
    "PREDICTION": PredictionAccount,
    "RESOLUTION": ResolutionAccount,
    "REPUTATION": ReputationAccount,
    "DISTRIBUTION": DistributionAccount,
    "BURN": BurnAccount,
    "STAKE": StakeAccount,
    "CONFIG": ConfigAccount,
    "ORACLE": OracleAccount,
}


def decode_account(raw: bytes) -> Any:
    """Route a raw account blob to its dataclass by discriminator byte."""
    if not raw:
        raise ValueError("empty account data")
    disc = raw[0]
    for name, cls in ACCOUNT_CLASSES.items():
        if disc == ACCOUNT_DISCRIMINATORS[name]:
            return cls.from_bytes(raw)
    raise ValueError(f"unknown discriminator 0x{disc:02x}")


# ---------------------------------------------------------------------------
# Nostr <-> Solana cross-reference builders (kind 30007)
# ---------------------------------------------------------------------------

def build_cross_reference(nostr_event_id: str, nostr_kind: int,
                          tx_signature: str, account_type: str,
                          account_address: str,
                          market_id: Optional[str] = None,
                          note: str = "") -> Dict[str, Any]:
    """kind 30007: anchors a Nostr event to its Solana tx + account so either
    chain can be verified independently."""
    tags = [
        ["e", nostr_event_id, "", "root"],
        ["k", str(nostr_kind)],
        ["t", "portent"],
        ["a", account_type],
        ["sol", tx_signature, account_address],
    ]
    if market_id:
        tags.append(["m", market_id])
    return {
        "kind": KIND_CROSS_REFERENCE,
        "tags": tags,
        "content": note or f"portent:{account_type.lower()}:{account_address[:8]}",
    }


# ---------------------------------------------------------------------------
# pump.fun launch params
# ---------------------------------------------------------------------------

def generate_pumpfun_launch_params() -> Dict[str, Any]:
    tok = load_tokenomics()
    launch = tok.get("launch", {})
    return {
        "name": NAME,
        "symbol": SYMBOL,
        "total_supply": TOTAL_SUPPLY,
        "decimals": DECIMALS,
        "initial_mc_target_sol": launch.get("initial_mc_target_sol", 50000),
        "curve": "pump.fun bonding curve",
        "raydium_lock": launch.get("liquidity",
                                   "bonding curve completion -> Raydium LP lock"),
        "metadata": {
            "total_supply": TOTAL_SUPPLY,
            "decimals": DECIMALS,
            "tokenomics_url": launch.get(
                "metadata_tokenomics_url",
                "https://raw.githubusercontent.com/cryptonomicsed-byte/"
                "Portent/main/tokenomics.json"),
            "buzz_integration": True,
            "nostr_native": True,
            "event_kinds": sorted(SUPPORTED_KINDS),
        },
        "staging": launch.get("steps", []),
    }


# ---------------------------------------------------------------------------
# Reputation math (shared by relay, sdk, agent, dashboard)
# ---------------------------------------------------------------------------

def tier_for(composite: int, resolved: int) -> str:
    for min_c, min_r, name in TIER_THRESHOLDS:
        if composite >= min_c and resolved >= min_r:
            return name
    return "unverified"


def composite_score(hit_rate_bp: int, resolved: int, accuracy: float,
                    disputes_lost: int = 0) -> int:
    """0..1000 blend: hit rate (60%), depth (25%), accuracy (15%), minus
    dispute penalty (max 100)."""
    if resolved == 0:
        return 0
    hit = hit_rate_bp / 100.0                    # 0..100
    depth = min(100.0, resolved / 50.0 * 100.0)  # 50 resolved = full depth
    acc = min(100.0, accuracy * 100.0)
    score = 0.60 * hit + 0.25 * depth + 0.15 * acc
    penalty = min(100.0, disputes_lost * 25.0)
    return max(0, min(1000, int(round(score - penalty))))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _manifest() -> Dict[str, Any]:
    out = {}
    for name, cls in ACCOUNT_CLASSES.items():
        inst = cls()
        out[name] = {
            "discriminator": f"0x{ACCOUNT_DISCRIMINATORS[name]:02x}",
            "size_bytes": inst.size(),
            "fields": list(inst.__dataclass_fields__.keys()),
        }
    return out


if __name__ == "__main__":
    print(json.dumps({
        "name": NAME,
        "symbol": SYMBOL,
        "supply": TOTAL_SUPPLY,
        "decimals": DECIMALS,
        "account_manifest": _manifest(),
        "launch_params": generate_pumpfun_launch_params(),
    }, indent=2))
