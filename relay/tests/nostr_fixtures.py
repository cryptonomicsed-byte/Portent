"""Test-only BIP-340 signer and Nostr event builder (fixtures; keys are tiny
test scalars, never real identities)."""
import hashlib
import json

from relay import bip340


def xonly(seckey: int) -> str:
    return bip340.point_mul(bip340.G, seckey)[0].to_bytes(32, "big").hex()


def sign(seckey: int, msg: bytes, aux: bytes = b"\x00" * 32) -> bytes:
    d0 = seckey
    pt = bip340.point_mul(bip340.G, d0)
    d = d0 if pt[1] % 2 == 0 else bip340.N - d0
    t = (d ^ int.from_bytes(bip340.tagged_hash("BIP0340/aux", aux), "big")).to_bytes(32, "big")
    px = pt[0].to_bytes(32, "big")
    k0 = int.from_bytes(bip340.tagged_hash("BIP0340/nonce", t + px + msg), "big") % bip340.N
    rpt = bip340.point_mul(bip340.G, k0)
    k = k0 if rpt[1] % 2 == 0 else bip340.N - k0
    rx = rpt[0].to_bytes(32, "big")
    e = int.from_bytes(bip340.tagged_hash("BIP0340/challenge", rx + px + msg), "big") % bip340.N
    return rx + ((k + e * d) % bip340.N).to_bytes(32, "big")


def event(seckey: int, kind: int, tags, content: str, created_at: int, sig_key=None):
    pk = xonly(seckey)
    body = json.dumps([0, pk, kind, tags, content, created_at], separators=(",", ":"))
    eid = hashlib.sha256(body.encode()).hexdigest()
    sig = sign(sig_key if sig_key is not None else seckey, bytes.fromhex(eid)).hex()
    return {"id": eid, "pubkey": pk, "kind": kind, "tags": tags, "content": content,
            "created_at": created_at, "sig": sig}
