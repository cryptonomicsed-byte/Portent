#!/usr/bin/env python3
"""
Portent SDK — the single agent-facing surface.

Components:
  events     — builders for all 8 Portent kinds (NIP-01 wire format)
  signer     — secp256k1 signer (coincurve if available, else documented stub)
               + NIP-19 bech32 npub/nsec helpers (stdlib bech32)
  client     — RelayClient: POST events, query /stats /leaderboard /predictions
               /reputation /resolutions /governance /oracles
  market     — odds math, payout, fee split, expiry rules
  reputation — composite score + tier ladder (portable, keyed by pubkey)
  token      — staking APR/compound, buyback schedule, burn accounting

Stdlib-only. Real signing + websocket transport are documented stubs, exactly
like the PROOF SDK: wire formats are NIP-01 and id binding is implemented, so
events produced here are relay-valid the moment a real signer is dropped in.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time
import urllib.request
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    from contracts.portent_program import (
        canonical_event_id, composite_score as _composite, tier_for as _tier,
        TIER_MULTIPLIERS, MIN_STAKE, MAX_STAKE, DEFAULT_STAKE,
        KIND_CROSS_REFERENCE, KIND_ATTESTATION, KIND_REPUTATION_SNAPSHOT,
        KIND_STAKE_CLAIM, KIND_GOVERNANCE, KIND_PREDICTION_POST,
        KIND_RESOLUTION, KIND_DISPUTE,
    )
except ImportError:  # pragma: no cover - standalone fallback
    KIND_CROSS_REFERENCE, KIND_ATTESTATION = 30007, 30009
    KIND_REPUTATION_SNAPSHOT, KIND_STAKE_CLAIM = 30010, 30011
    KIND_GOVERNANCE, KIND_PREDICTION_POST = 30012, 30015
    KIND_RESOLUTION, KIND_DISPUTE = 30016, 30017
    TIER_MULTIPLIERS = {"unverified": 1.0, "verified": 1.5, "trusted": 2.0,
                        "elite": 2.5, "legendary": 3.0}
    MIN_STAKE, MAX_STAKE, DEFAULT_STAKE = 100, 5000, 1000

    def canonical_event_id(pubkey_hex, kind, tags, content, created_at):
        payload = json.dumps([0, pubkey_hex, kind, tags, content, created_at],
                             separators=(",", ":"))
        return hashlib.sha256(payload.encode()).hexdigest()

    def _tier(composite, resolved):
        return "unverified"

    def _composite(hit_rate_bp, resolved, accuracy, disputes_lost=0):
        return 0


DEFAULT_RELAY = os.environ.get("PORTENT_RELAY", "http://127.0.0.1:8899")


# ---------------------------------------------------------------------------
# bech32 (BIP-173) — used for NIP-19 npub/nsec
# ---------------------------------------------------------------------------

BECH32_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"


def _bech32_polymod(values: List[int]) -> int:
    GEN = (0x3b6a57b2, 0x26508e6d, 0x1ea119fa, 0x3d4233dd, 0x2a1462b3)
    chk = 1
    for v in values:
        b = chk >> 25
        chk = (chk & 0x1ffffff) << 5 ^ v
        for i in range(5):
            chk ^= GEN[i] if ((b >> i) & 1) else 0
    return chk


def _bech32_hrp_expand(hrp: str) -> List[int]:
    return [ord(x) >> 5 for x in hrp] + [0] + [ord(x) & 31 for x in hrp]


def _bech32_verify_checksum(hrp: str, data: List[int]) -> bool:
    return _bech32_polymod(_bech32_hrp_expand(hrp) + data) == 1


def _bech32_create_checksum(hrp: str, data: List[int]) -> List[int]:
    values = _bech32_hrp_expand(hrp) + data
    polymod = _bech32_polymod(values + [0, 0, 0, 0, 0, 0]) ^ 1
    return [(polymod >> 5 * (5 - i)) & 31 for i in range(6)]


def _convertbits(data: bytes, frombits: int, tobits: int, pad: bool = True):
    acc = 0
    bits = 0
    ret = []
    maxv = (1 << tobits) - 1
    for value in data:
        acc = (acc << frombits) | value
        bits += frombits
        while bits >= tobits:
            bits -= tobits
            ret.append((acc >> bits) & maxv)
    if pad and bits:
        ret.append((acc << (tobits - bits)) & maxv)
    return ret


def bech32_encode(hrp: str, data: bytes) -> str:
    conv = _convertbits(data, 8, 5)
    combined = conv + _bech32_create_checksum(hrp, conv)
    return hrp + "1" + "".join(BECH32_CHARSET[d] for d in combined)


def bech32_decode(bech: str):
    pos = bech.rfind("1")
    if pos < 1:
        raise ValueError("invalid bech32")
    hrp = bech[:pos]
    data = [BECH32_CHARSET.find(c) for c in bech[pos + 1:]]
    if any(d < 0 for d in data) or not _bech32_verify_checksum(hrp, data):
        raise ValueError("bad checksum")
    return hrp, bytes(_convertbits(data[:-6], 5, 8, pad=False))


def encode_npub(pubkey_hex: str) -> str:
    return bech32_encode("npub", bytes.fromhex(pubkey_hex))


def decode_npub(npub: str) -> str:
    hrp, raw = bech32_decode(npub)
    if hrp != "npub":
        raise ValueError(f"expected npub, got {hrp}")
    return raw.hex()


def encode_nsec(privkey_hex: str) -> str:
    return bech32_encode("nsec", bytes.fromhex(privkey_hex))


# ---------------------------------------------------------------------------
# Signer
# ---------------------------------------------------------------------------

class Signer:
    """Ed25519-ish Nostr signer. Uses coincurve if importable; otherwise emits
    a structurally-valid stub signature and marks it UNSIGNED_STUB. The event
    id (sha256 over canonical serialization) is ALWAYS correct — swap in a
    real lib and everything downstream validates unchanged."""

    def __init__(self, privkey_hex: str = ""):
        self.privkey_hex = privkey_hex or "00" * 32
        try:
            from coincurve import PrivateKey  # type: ignore
            self._impl = "coincurve"
            self._pk = PrivateKey(bytes.fromhex(self.privkey_hex))
            self.pubkey_hex = self._pk.public_key.format().hex()
        except Exception:  # noqa: BLE001 - documented stub path
            self._impl = "stub"
            # secp256k1 pubkey from private scalar: x = G*k (documented stub —
            # deterministic placeholder derived from the scalar so pubkeys are
            # stable per key without a real EC lib).
            h = hashlib.sha256(bytes.fromhex(self.privkey_hex)).digest()
            self.pubkey_hex = h.hex()
        self.npub = encode_npub(self.pubkey_hex)

    def sign(self, event_id: str) -> str:
        if self._impl == "coincurve":
            return self._pk.sign(b"\x00" * 32 + bytes.fromhex(event_id),
                                 hasher=None).hex()
        return "00" * 64 + " [UNSIGNED_STUB: coincurve not installed]"

    @property
    def is_stub(self) -> bool:
        return self._impl == "stub"


# ---------------------------------------------------------------------------
# Event builders
# ---------------------------------------------------------------------------

class Events:
    @staticmethod
    def _base(signer: Signer, kind: int, tags: List[List[str]],
              content: str = "", created_at: Optional[int] = None) -> Dict[str, Any]:
        ts = int(created_at or time.time())
        ev = {
            "pubkey": signer.pubkey_hex,
            "kind": kind,
            "tags": tags,
            "content": content,
            "created_at": ts,
        }
        ev["id"] = canonical_event_id(signer.pubkey_hex, kind, tags,
                                      content, ts)
        ev["sig"] = signer.sign(ev["id"])
        return ev

    @staticmethod
    def prediction(signer: Signer, market_id: str, side: str,
                   stake: int = DEFAULT_STAKE, odds_bp: int = 15000,
                   event_class: str = "general",
                   deadline: Optional[int] = None,
                   note: str = "") -> Dict[str, Any]:
        stake = max(MIN_STAKE, min(MAX_STAKE, stake))
        d = time.time() + 7 * 86400 if deadline is None else deadline
        tags = [
            ["d", f"portent:pred:{market_id[:16]}:{signer.pubkey_hex[:12]}"],
            ["m", market_id],
            ["side", side],
            ["stake", str(stake)],
            ["odds", str(odds_bp)],
            ["deadline", str(int(d))],
            ["c", event_class],
        ]
        content = json.dumps({"note": note, "side": side, "stake": stake,
                              "odds_bp": odds_bp}) if note else ""
        return Events._base(signer, KIND_PREDICTION_POST, tags, content)

    @staticmethod
    def resolution(signer: Signer, market_id: str, outcome: str,
                   confidence: float = 0.8, note: str = "") -> Dict[str, Any]:
        tags = [
            ["d", f"portent:res:{market_id[:16]}:{signer.pubkey_hex[:12]}"],
            ["m", market_id],
            ["outcome", outcome],
        ]
        content = json.dumps({"confidence": confidence, "note": note})
        return Events._base(signer, KIND_RESOLUTION, tags, content)

    @staticmethod
    def dispute(signer: Signer, market_id: str, reason: str) -> Dict[str, Any]:
        tags = [
            ["d", f"portent:dis:{market_id[:16]}:{signer.pubkey_hex[:12]}"],
            ["m", market_id],
        ]
        return Events._base(signer, KIND_DISPUTE, tags, reason[:500])

    @staticmethod
    def cross_reference(signer: Signer, nostr_event_id: str, nostr_kind: int,
                        tx_signature: str, account_type: str,
                        account_address: str,
                        market_id: Optional[str] = None) -> Dict[str, Any]:
        tags = [
            ["d", f"portent:xref:{nostr_event_id[:16]}:{signer.pubkey_hex[:12]}"],
            ["e", nostr_event_id, "", "root"],
            ["k", str(nostr_kind)],
            ["a", account_type],
            ["sol", tx_signature, account_address],
        ]
        if market_id:
            tags.append(["m", market_id])
        return Events._base(signer, KIND_CROSS_REFERENCE, tags)

    @staticmethod
    def attestation(signer: Signer, market_id: str, kind_label: str,
                    quality: float = 1.0) -> Dict[str, Any]:
        """kind 30009 — attestation of oracle/cross-check work."""
        tags = [
            ["d", f"portent:att:{market_id[:16]}:{signer.pubkey_hex[:12]}"],
            ["m", market_id],
            ["work", kind_label],
            ["quality", str(quality)],
        ]
        content = json.dumps({"quality": quality, "work": kind_label})
        return Events._base(signer, KIND_ATTESTATION, tags, content)

    @staticmethod
    def reputation_snapshot(signer: Signer, stats: Dict[str, Any],
                            linked_communities: Optional[List[str]] = None
                            ) -> Dict[str, Any]:
        tags = [
            ["d", f"portent:rep:{signer.pubkey_hex[:16]}"],
            ["p", signer.pubkey_hex],
        ]
        for comm in (linked_communities or ["buzz:main"]):
            tags.append(["c", comm])
        content = json.dumps(stats)
        return Events._base(signer, KIND_REPUTATION_SNAPSHOT, tags, content)

    @staticmethod
    def stake_claim(signer: Signer, amount: int, tier: str,
                    lock_until: Optional[int] = None) -> Dict[str, Any]:
        tags = [
            ["d", f"portent:stake:{signer.pubkey_hex[:16]}"],
            ["p", signer.pubkey_hex],
        ]
        content = json.dumps({
            "amount": amount, "tier": tier,
            "lock_until": int(lock_until or time.time() + 90 * 86400),
        })
        return Events._base(signer, KIND_STAKE_CLAIM, tags, content)

    @staticmethod
    def governance_proposal(signer: Signer, ptype: str, title: str,
                            quorum_pct: int = 3,
                            voting_window_hours: int = 72) -> Dict[str, Any]:
        tags = [
            ["d", f"portent:gov:{signer.pubkey_hex[:16]}:{int(time.time())}"],
            ["p", signer.pubkey_hex],
        ]
        content = json.dumps({"ptype": ptype, "title": title,
                              "quorum_pct": quorum_pct,
                              "voting_window_hours": voting_window_hours})
        return Events._base(signer, KIND_GOVERNANCE, tags, content)

    @staticmethod
    def governance_vote(signer: Signer, proposal_id: str,
                        side: str = "for") -> Dict[str, Any]:
        tags = [
            ["d", f"portent:vote:{signer.pubkey_hex[:16]}:{proposal_id}"],
            ["vote", proposal_id, side],
        ]
        return Events._base(signer, KIND_GOVERNANCE, tags)


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class RelayClient:
    def __init__(self, base: str = DEFAULT_RELAY):
        self.base = base.rstrip("/")

    def _get(self, path: str) -> Dict[str, Any]:
        with urllib.request.urlopen(f"{self.base}{path}",
                                    timeout=15) as resp:
            return json.loads(resp.read().decode())

    def post_event(self, ev: Dict[str, Any]) -> Dict[str, Any]:
        req = urllib.request.Request(
            f"{self.base}/event",
            data=json.dumps({"event": ev}).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode())

    def stats(self) -> Dict[str, Any]:
        return self._get("/stats")

    def leaderboard(self) -> List[Dict[str, Any]]:
        return self._get("/leaderboard")["leaderboard"]

    def predictions(self, status: str = "open") -> List[Dict[str, Any]]:
        return self._get(f"/predictions?status={status}")["predictions"]

    def market(self, market_id: str) -> Dict[str, Any]:
        return self._get(f"/prediction/{market_id}")

    def reputation(self, pubkey: str) -> Optional[Dict[str, Any]]:
        return self._get(f"/reputation/{pubkey}")["reputation"]

    def resolutions(self) -> List[Dict[str, Any]]:
        return self._get("/resolutions")["resolutions"]

    def governance(self) -> List[Dict[str, Any]]:
        return self._get("/governance")["proposals"]

    def oracles(self) -> List[Dict[str, Any]]:
        return self._get("/oracles")["oracles"]

    def events(self, kind: Optional[int] = None) -> List[Dict[str, Any]]:
        path = f"/events?kind={kind}" if kind else "/events"
        return self._get(path)["events"]


# ---------------------------------------------------------------------------
# market — odds, payout, fee-split math
# ---------------------------------------------------------------------------

class Market:
    # 10000bp = even money; 20000bp = 2.0x (double-or-nothing).
    ODDS_MENU = (11000, 12000, 15000, 20000, 30000, 40000)

    @staticmethod
    def suggested_odds(prob: float) -> int:
        """Map an estimated win probability (0..1) to the closest menu odds.
        Fair decimal odds = 1/prob; in bp that is 10000/prob."""
        fair = int(10000 / max(0.05, min(0.95, prob)))
        return min(Market.ODDS_MENU, key=lambda o: abs(o - fair))

    @staticmethod
    def profit(stake: int, odds_bp: int) -> int:
        return max(0, stake * (odds_bp - 10000) // 10000)

    @staticmethod
    def fee(payout: int, fee_bps: int = 200) -> int:
        return payout * fee_bps // 10000

    @staticmethod
    def fee_split(fee_tokens: int, burn_bps: int = 200,
                  reserve_pct: int = 50) -> Dict[str, int]:
        burn = fee_tokens * burn_bps // 10000
        remainder = fee_tokens - burn
        reserve = remainder * reserve_pct // 100
        stakers = remainder - reserve
        return {"burn": burn, "buyback_reserve": reserve,
                "staker_pool": stakers}

    @staticmethod
    def expire_burn(stake: int) -> int:
        return stake  # full stake forfeited -> burn


# ---------------------------------------------------------------------------
# reputation — portable, keyed by pubkey
# ---------------------------------------------------------------------------

class Reputation:
    @staticmethod
    def compute(hit_rate_bp: int, resolved: int, accuracy: float,
                disputes_lost: int = 0) -> int:
        return _composite(hit_rate_bp, resolved, accuracy, disputes_lost)

    @staticmethod
    def tier(composite: int, resolved: int) -> str:
        return _tier(composite, resolved)

    @staticmethod
    def multiplier(tier: str) -> float:
        return TIER_MULTIPLIERS.get(tier, 1.0)

    @staticmethod
    def weighted_reward(base: int, quality: float, tier: str) -> int:
        return int(base * max(0.5, min(3.0, quality))
                   * Reputation.multiplier(tier))


# ---------------------------------------------------------------------------
# token — staking, buyback, burn accounting
# ---------------------------------------------------------------------------

class Token:
    STAKING_TIERS = ((30, 8.0), (90, 12.0), (365, 20.0))

    @staticmethod
    def apr_for(lock_days: int) -> float:
        for days, apr in Token.STAKING_TIERS:
            if days == lock_days:
                return apr
        return 0.0

    @staticmethod
    def compound(amount: int, apr_pct: float, days: float,
                 decimals: int = 9) -> int:
        """Continuous-compounding approximation for the auto-compound tier."""
        rate = apr_pct / 100.0
        return int(amount * (2.718281828 ** (rate * days / 365.0)))

    @staticmethod
    def buyback_schedule(reserve_sol: float) -> Optional[Dict[str, float]]:
        """Daily Jupiter limit order: min 0.1 / max 5.0 SOL, 100bps slippage,
        auto-trigger once reserve >= 100 SOL."""
        if reserve_sol < 100.0:
            return None
        size = min(5.0, max(0.1, reserve_sol * 0.02))
        return {"size_sol": round(size, 4), "slippage_bps": 100,
                "frequency": "daily", "method": "jupiter limit order",
                "destiny": "burned"}


# ---------------------------------------------------------------------------
# CLI smoke test
# ---------------------------------------------------------------------------

def _demo() -> None:
    import random
    signer = Signer()
    client = RelayClient()
    print(f"pubkey   : {signer.pubkey_hex}")
    print(f"npub     : {signer.npub}")
    print(f"signer   : {signer._impl} "
          f"({'stub — coincurve absent' if signer.is_stub else 'real secp256k1'})")

    market = hashlib.sha256(b"SOL closes > $250 by EOD").hexdigest()
    ev = Events.prediction(signer, market, "yes", stake=1000, odds_bp=2000,
                           event_class="token", note="demo prediction")
    print(f"\nprediction event id : {ev['id']}")
    print(f"  id-valid           : "
          f"{canonical_event_id(signer.pubkey_hex, ev['kind'], ev['tags'], ev['content'], ev['created_at']) == ev['id']}")
    print(f"  post               : {client.post_event(ev)}")

    odds = Market.suggested_odds(0.6)
    print(f"\nsuggested odds p=0.6 : {odds}bp "
          f"(profit {Market.profit(1000, odds)})")
    # realistic payout: stake 1000 @ 15000bp -> payout 1500 -> fee 30
    fee = Market.fee(1500)
    print(f"fee split on payout 1500 (fee {fee}): {Market.fee_split(fee)}")
    print(f"npub roundtrip       : {decode_npub(signer.npub) == signer.pubkey_hex}")


if __name__ == "__main__":
    _demo()
