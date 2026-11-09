"""P3-04: Portent settles only from Crucible oracle keys.

Flag PORTENT_CRUCIBLE_ONLY (default off). Off = this module is never entered
by the relay and settlement behaves exactly as before.

On = a 30016 resolution settles a market only when ALL hold:
  1. its pubkey is in the Crucible oracle allowlist, built from
       PORTENT_CRUCIBLE_KEYS    comma-separated 64-hex x-only pubkeys, and/or
       PORTENT_CRUCIBLE_POLICY  path to a Crucible policy JSON; its
                                `oracle_authorities` list is read
  2. its id is the canonical NIP-01 id of its contents, and
  3. its `sig` is a valid BIP-340 Schnorr signature by that pubkey over the
     id. This is checked here regardless of PORTENT_REQUIRE_SIG, because a
     key allowlist without a signature check only checks a string field.

Fail-closed: flag on with an empty allowlist means nobody settles. A refused
resolution is still stored as an event (as before) and recorded in
crucible_refused_resolutions; it settles nothing and creates no oracle row.

Which key Crucible's authority is long term is an open decision; this module
only reads whatever list it is given.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import sys
from typing import Any, Dict, FrozenSet, Tuple

try:
    from relay import bip340  # repo root on sys.path
except ImportError:  # pragma: no cover - run from relay/
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import bip340  # type: ignore[no-redef]

ENV_FLAG = "PORTENT_CRUCIBLE_ONLY"
ENV_KEYS = "PORTENT_CRUCIBLE_KEYS"
ENV_POLICY = "PORTENT_CRUCIBLE_POLICY"

_HEX64 = re.compile(r"^[0-9a-f]{64}$")

REFUSALS_SCHEMA = """
CREATE TABLE IF NOT EXISTS crucible_refused_resolutions(
    event_id TEXT PRIMARY KEY,
    market_id TEXT NOT NULL,
    oracle TEXT NOT NULL,
    outcome TEXT NOT NULL,
    confidence REAL NOT NULL,
    reason TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
"""


def enabled() -> bool:
    return os.environ.get(ENV_FLAG, "").strip().lower() in ("1", "true", "yes", "on")


def _norm(keys) -> FrozenSet[str]:
    out = set()
    for k in keys:
        k = str(k).strip().lower()
        if not k:
            continue
        if _HEX64.match(k):
            out.add(k)
        else:
            print(f"WARNING: ignoring malformed Crucible key {k[:12]}...",
                  file=sys.stderr)
    return frozenset(out)


def crucible_keys() -> FrozenSet[str]:
    """Union of PORTENT_CRUCIBLE_KEYS and the policy file's oracle_authorities.
    Read per call so a policy change needs no restart."""
    keys = list(os.environ.get(ENV_KEYS, "").split(","))
    path = os.environ.get(ENV_POLICY, "").strip()
    if path:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                policy = json.load(fh)
            auth = policy.get("oracle_authorities", []) if isinstance(policy, dict) else []
            if isinstance(auth, list):
                keys.extend(auth)
        except (OSError, ValueError) as exc:
            print(f"WARNING: cannot read {ENV_POLICY}: {exc}", file=sys.stderr)
    return _norm(keys)


def _canonical_ids(ev: Dict[str, Any]) -> FrozenSet[str]:
    """Both serialisations that bind the same content: Portent's own
    (contracts.canonical_event_id, ASCII-escaped) and strict NIP-01 (raw
    UTF-8). Identical for ASCII content."""
    body = [0, ev["pubkey"], ev["kind"], ev["tags"], ev["content"], ev["created_at"]]
    return frozenset(
        hashlib.sha256(json.dumps(body, separators=(",", ":"),
                                  ensure_ascii=ea).encode("utf-8")).hexdigest()
        for ea in (True, False))


def check_resolution(ev: Dict[str, Any]) -> Tuple[bool, str]:
    """(may_settle, reason). Only meaningful when enabled()."""
    keys = crucible_keys()
    if not keys:
        return False, "no_crucible_keys_configured"
    pubkey = str(ev.get("pubkey", "")).lower()
    if pubkey not in keys:
        return False, "unlisted_key"
    try:
        if str(ev.get("id", "")).lower() not in _canonical_ids(ev):
            return False, "bad_event_id"
    except (KeyError, TypeError, ValueError):
        return False, "bad_event_id"
    if not bip340.verify_hex(pubkey, str(ev.get("id", "")), str(ev.get("sig", ""))):
        return False, "bad_signature"
    return True, "crucible_key"


def record_refusal(c: sqlite3.Connection, ev: Dict[str, Any], market: str,
                   outcome: str, confidence: float, reason: str) -> None:
    c.executescript(REFUSALS_SCHEMA)
    c.execute(
        "INSERT OR IGNORE INTO crucible_refused_resolutions(event_id, market_id, "
        "oracle, outcome, confidence, reason, created_at) VALUES(?,?,?,?,?,?,?)",
        (ev.get("id", ""), market, ev.get("pubkey", ""), outcome, confidence,
         reason, int(ev.get("created_at", 0))))
