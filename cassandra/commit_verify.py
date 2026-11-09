"""Commit-then-verify ledger (from Cassandra's blockchain/prediction-logger.ts).

The original hashed the prediction and then "submitted" it by inventing a
random transaction hash. Here the commitment is real and checkable:

  commit(prediction)  -> CommitmentProof (published) + salt (kept private)
      prediction_hash = sha256(canonical prediction JSON)
      reasoning_hash  = sha256(canonical signals JSON)
      commitment      = sha256(salt || prediction_hash || reasoning_hash)
  reveal(proof, prediction, salt) -> True only if everything recomputes
  validate(prediction_id, outcome) -> one-shot; a validated prediction can't
      be re-validated, and only a revealed commitment can be validated.

Optional: anchor_event(proof) builds an unsigned Portent kind-30009
(attestation) event body for the commitment, so it can be timestamped on the
relay by whoever holds a key. Nothing here signs or sends.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from typing import Any, Dict, Optional, Tuple

from .types import CommitmentProof, ParadigmPrediction

KIND_ATTESTATION = 30009


def _canon(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def prediction_hash(p: ParadigmPrediction) -> str:
    d = p.to_dict()
    d.pop("status", None)  # status changes over the lifecycle; the claim doesn't
    return _sha(_canon(d))


def reasoning_hash(p: ParadigmPrediction) -> str:
    return _sha(_canon(p.to_dict()["signals"]))


def commitment_of(salt_hex: str, p_hash: str, r_hash: str) -> str:
    return _sha(bytes.fromhex(salt_hex) + bytes.fromhex(p_hash) + bytes.fromhex(r_hash))


class CommitLedger:
    def __init__(self, path: str = ":memory:") -> None:
        self.db = sqlite3.connect(path)
        self.db.execute("""CREATE TABLE IF NOT EXISTS commitments (
            proof_id TEXT PRIMARY KEY, prediction_id TEXT UNIQUE NOT NULL,
            prediction_hash TEXT NOT NULL, reasoning_hash TEXT NOT NULL,
            commitment TEXT NOT NULL, confidence_weight REAL NOT NULL,
            committed_at INTEGER NOT NULL, revealed_at INTEGER,
            validated_at INTEGER, outcome INTEGER, lead_time_s INTEGER)""")
        self.db.commit()

    def commit(self, p: ParadigmPrediction, now: Optional[int] = None) -> Tuple[CommitmentProof, str]:
        if self.db.execute("SELECT 1 FROM commitments WHERE prediction_id=?", (p.id,)).fetchone():
            raise ValueError(f"prediction {p.id} already committed")
        salt = os.urandom(32).hex()
        ph, rh = prediction_hash(p), reasoning_hash(p)
        c = commitment_of(salt, ph, rh)
        proof = CommitmentProof(proof_id=c[:16], prediction_id=p.id, prediction_hash=ph,
                                reasoning_hash=rh, commitment=c,
                                confidence_weight=float(p.confidence.value) * float(p.probability),
                                committed_at=int(now or time.time()))
        self.db.execute("INSERT INTO commitments (proof_id, prediction_id, prediction_hash, reasoning_hash, "
                        "commitment, confidence_weight, committed_at) VALUES (?,?,?,?,?,?,?)",
                        (proof.proof_id, p.id, ph, rh, c, proof.confidence_weight, proof.committed_at))
        self.db.commit()
        return proof, salt

    @staticmethod
    def verify(proof: CommitmentProof, p: ParadigmPrediction, salt_hex: str) -> bool:
        ph, rh = prediction_hash(p), reasoning_hash(p)
        return (ph == proof.prediction_hash and rh == proof.reasoning_hash
                and commitment_of(salt_hex, ph, rh) == proof.commitment and p.id == proof.prediction_id)

    def reveal(self, proof: CommitmentProof, p: ParadigmPrediction, salt_hex: str,
               now: Optional[int] = None) -> bool:
        row = self.db.execute("SELECT commitment FROM commitments WHERE proof_id=?", (proof.proof_id,)).fetchone()
        if not row or row[0] != proof.commitment or not self.verify(proof, p, salt_hex):
            return False
        self.db.execute("UPDATE commitments SET revealed_at=COALESCE(revealed_at, ?) WHERE proof_id=?",
                        (int(now or time.time()), proof.proof_id))
        self.db.commit()
        return True

    def validate(self, prediction_id: str, outcome: bool, now: Optional[int] = None) -> Dict[str, Any]:
        row = self.db.execute("SELECT committed_at, revealed_at, validated_at FROM commitments "
                              "WHERE prediction_id=?", (prediction_id,)).fetchone()
        if not row:
            raise KeyError(prediction_id)
        if row[1] is None:
            raise ValueError("reveal before validating")
        if row[2] is not None:
            raise ValueError("already validated")
        now = int(now or time.time())
        self.db.execute("UPDATE commitments SET validated_at=?, outcome=?, lead_time_s=? WHERE prediction_id=?",
                        (now, int(bool(outcome)), now - row[0], prediction_id))
        self.db.commit()
        return {"prediction_id": prediction_id, "outcome": bool(outcome), "lead_time_s": now - row[0]}

    def accuracy(self) -> Dict[str, Any]:
        n, ok, lead = self.db.execute(
            "SELECT COUNT(*), COALESCE(SUM(outcome),0), AVG(lead_time_s) FROM commitments "
            "WHERE validated_at IS NOT NULL").fetchone()
        total = self.db.execute("SELECT COUNT(*) FROM commitments").fetchone()[0]
        return {"committed": total, "validated": n, "correct": ok,
                "accuracy": (ok / n) if n else None,
                "average_lead_time_days": (lead / 86400.0) if lead is not None else None}


def anchor_event(proof: CommitmentProof, pubkey_hex: str, now: Optional[int] = None) -> Dict[str, Any]:
    """Unsigned Portent 30009 body for a commitment (caller signs + sends)."""
    return {"pubkey": pubkey_hex, "kind": KIND_ATTESTATION, "created_at": int(now or time.time()),
            "tags": [["d", f"cassandra:{proof.proof_id}"], ["commitment", proof.commitment],
                     ["prediction", proof.prediction_id]],
            "content": json.dumps({"type": "cassandra_commitment", "proof": proof.to_dict()}, sort_keys=True)}
