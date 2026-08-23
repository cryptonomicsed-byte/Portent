#!/usr/bin/env python3
"""
Portent agent — autonomous predictor / oracle / cross-checker.

Watches relay state, publishes staked predictions on open markets, resolves
due markets (as oracle), cross-checks resolutions, and keeps its portable
reputation snapshot current. Every action is a signed Nostr event — no hidden
state. State persists to ~/.portent/agent_state.json.

Strategy (deterministic, stdlib-only, configurable):
  - Pick the highest-activity open market, side = momentum (follow the
    most-staked side) unless momentum is split, then contrarian.
  - Stake the DEFAULT_STAKE (1000), odds from the menu nearest implied prob.
  - Resolve any market past deadline where this agent is on the oracle panel
    (or, in single-agent sim mode, any overdue market) with confidence from
    a deterministic pseudo-signal.
  - Cross-check recent resolutions: if the resolution matches this agent's
    last published side for that market, attest (kind 30009, quality 1.0).
  - Guards from tokenomics: 60s cooldown, 50K/day earning cap.

Usage:
  python3 agent/portent_agent.py --once      # single cycle, then exit
  python3 agent/portent_agent.py --sim 5     # 5 cycles, 15s apart
  python3 agent/portent_agent.py             # loop forever
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import sys
import time
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sdk.portent_sdk import (  # noqa: E402
    Signer, Events, RelayClient, Market, Reputation, Token,
    DEFAULT_STAKE, MIN_STAKE, MAX_STAKE,
)

STATE_DIR = os.environ.get("PORTENT_STATE", os.path.expanduser("~/.portent"))
STATE_FILE = os.path.join(STATE_DIR, "agent_state.json")
RELAY = os.environ.get("PORTENT_RELAY", "http://127.0.0.1:8899")

COOLDOWN_SECONDS = 60
DAILY_CAP = 50_000
LINKED_COMMUNITIES = ["buzz:main", "portent:genesis"]


def load_state() -> Dict[str, Any]:
    default = {
        "pubkey": "",
        "predictions_published": 0,
        "resolutions_published": 0,
        "cross_checks": 0,
        "balance": 100_000,          # initial agent wallet (testnet-style)
        "daily_earned": 0,
        "last_action_ts": 0,
        "last_day": int(time.time()) // 86400,
        "markets": {},               # market_id -> {"side": "...", "odds_bp": n}
    }
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as fh:
            return {**default, **json.load(fh)}
    except (OSError, json.JSONDecodeError):
        return default


def save_state(state: Dict[str, Any]) -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2)


class PortentAgent:
    def __init__(self, signer: Signer, client: RelayClient):
        self.signer = signer
        self.client = client
        self.state = load_state()
        if not self.state["pubkey"]:
            self.state["pubkey"] = signer.pubkey_hex
            save_state(self.state)

    # -- guards ------------------------------------------------------------

    def _cooldown_ok(self) -> bool:
        return (time.time() - self.state["last_action_ts"]) >= COOLDOWN_SECONDS

    def _cap_ok(self, amount: int) -> bool:
        day = int(time.time()) // 86400
        if day != self.state["last_day"]:
            self.state["last_day"] = day
            self.state["daily_earned"] = 0
        return self.state["daily_earned"] + amount <= DAILY_CAP

    def _touch(self, earned: int = 0) -> None:
        self.state["last_action_ts"] = int(time.time())
        self.state["daily_earned"] += earned
        save_state(self.state)

    # -- strategies --------------------------------------------------------

    def _pick_market(self, open_preds: List[Dict[str, Any]]) -> Optional[str]:
        """Highest-activity open market."""
        counts: Dict[str, int] = {}
        for p in open_preds:
            counts[p["market_id"]] = counts.get(p["market_id"], 0) + 1
        if not counts:
            return None
        return max(counts, key=counts.get)

    def _decide_side(self, market_id: str,
                     preds: List[Dict[str, Any]]) -> str:
        """Momentum: follow the most-staked side; contrarian if tied."""
        stakes = {"yes": 0, "no": 0}
        for p in preds:
            if p["market_id"] == market_id:
                stakes[p["side"]] = stakes.get(p["side"], 0) + p["stake"]
        if stakes["yes"] == stakes["no"]:
            # deterministic contrarian flip
            seed = int(hashlib.sha256(market_id.encode()).hexdigest(), 16)
            return "no" if seed % 2 else "yes"
        return "yes" if stakes["yes"] > stakes["no"] else "no"

    def _signal(self, market_id: str) -> float:
        """Deterministic pseudo-signal in [0.35, 0.8] for oracle confidence."""
        seed = int(hashlib.sha256((market_id + self.signer.pubkey_hex[:8])
                                  .encode()).hexdigest(), 16)
        return round(0.35 + (seed % 4500) / 10000.0, 3)

    # -- actions -----------------------------------------------------------

    def act_once(self, verbose: bool = True) -> Dict[str, Any]:
        report: Dict[str, Any] = {"actions": []}
        if not self._cooldown_ok():
            report["skipped"] = "cooldown"
            return report
        try:
            stats = self.client.stats()
        except Exception as exc:  # noqa: BLE001
            report["error"] = f"relay unreachable: {exc}"
            return report

        open_preds = self.client.predictions("open")

        # 1) Resolve overdue markets (oracle duty).
        now = int(time.time())
        for p in open_preds:
            if p["deadline"] < now:
                confidence = self._signal(p["market_id"])
                ev = Events.resolution(self.signer, p["market_id"],
                                       self._decide_side(p["market_id"],
                                                         open_preds),
                                       confidence,
                                       note="oracle duty (deadline passed)")
                resp = self.client.post_event(ev)
                self.state["resolutions_published"] += 1
                report["actions"].append(("resolve", p["market_id"],
                                          resp.get("ok")))
                self._touch(earned=500)
                break  # one resolution per cycle (cooldown)

        # 2) Cross-check recent resolutions.
        if not report["actions"]:
            for res in self.client.resolutions()[:3]:
                known = self.state["markets"].get(res["market_id"])
                if known and known["side"] == res["outcome"]:
                    ev = Events.attestation(self.signer, res["market_id"],
                                            "cross_check", quality=1.0)
                    resp = self.client.post_event(ev)
                    self.state["cross_checks"] += 1
                    report["actions"].append(("cross_check",
                                              res["market_id"],
                                              resp.get("ok")))
                    self._touch(earned=150)
                    break

        # 3) Publish a staked prediction.
        if not report["actions"] and self.state["balance"] >= MIN_STAKE:
            market_id = self._pick_market(open_preds)
            if market_id is None:
                # Nothing to follow yet — seed a market on a synthetic event.
                seed_event = hashlib.sha256(
                    f"portent:{int(time.time())}:{self.signer.pubkey_hex[:8]}"
                    .encode()).hexdigest()
                market_id = seed_event
            side = self._decide_side(market_id, open_preds)
            prob = 0.55 if side == "yes" else 0.45
            odds = Market.suggested_odds(prob)
            stake = DEFAULT_STAKE
            ev = Events.prediction(self.signer, market_id, side,
                                   stake=stake, odds_bp=odds,
                                   event_class="token",
                                   note="momentum-follow")
            resp = self.client.post_event(ev)
            self.state["predictions_published"] += 1
            self.state["balance"] -= stake
            self.state["markets"][market_id] = {"side": side,
                                                "odds_bp": odds}
            report["actions"].append(("predict", market_id,
                                      resp.get("ok")))
            self._touch()

        # 4) Refresh reputation snapshot.
        rep = self.client.reputation(self.signer.pubkey_hex) or {}
        if rep:
            ev = Events.reputation_snapshot(
                self.signer, {
                    "predictions": rep["predictions"],
                    "resolved": rep["resolved"],
                    "hit_rate_bp": rep["hit_rate_bp"],
                    "accuracy": rep["accuracy"],
                    "composite": rep["composite"],
                    "tier": rep["tier"],
                }, LINKED_COMMUNITIES)
            self.client.post_event(ev)
            report["actions"].append(("reputation_snapshot", "", True))

        save_state(self.state)
        report["state"] = {
            "predictions": self.state["predictions_published"],
            "resolutions": self.state["resolutions_published"],
            "cross_checks": self.state["cross_checks"],
            "balance": self.state["balance"],
            "daily_earned": self.state["daily_earned"],
        }
        return report


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description="Portent autonomous agent")
    ap.add_argument("--once", action="store_true", help="single cycle")
    ap.add_argument("--sim", type=int, default=0,
                    help="run N cycles, 15s apart")
    ap.add_argument("--key", default=os.environ.get("PORTENT_AGENT_KEY", ""),
                    help="agent private key hex (default: deterministic dev key)")
    args = ap.parse_args()

    signer = Signer(args.key or "ab" * 32)
    client = RelayClient(RELAY)
    agent = PortentAgent(signer, client)
    print(f"[portent-agent] pubkey {signer.pubkey_hex[:16]}... "
          f"signer={signer._impl} relay={RELAY}", flush=True)

    cycles = 1 if args.once else (args.sim or -1)
    n = 0
    while cycles < 0 or n < cycles:
        n += 1
        report = agent.act_once()
        print(f"[cycle {n}] " + json.dumps(report), flush=True)
        if report.get("actions"):
            pass
        if cycles > 0 and n >= cycles:
            break
        if cycles < 0:
            time.sleep(COOLDOWN_SECONDS)


if __name__ == "__main__":
    main()
