"""BIP-340 Schnorr signature verification over secp256k1, pure Python.

Nostr (NIP-01) signs the 32-byte event id with BIP-340 Schnorr. The relay's
`_verify_schnorr_sig` (E-28) re-encodes the signature as ECDSA, which is not
the same scheme, so a genuine Nostr signature does not verify there. The
Crucible settlement gate (P3-04) needs a real answer to "did this key sign
this event", so it uses this module instead.

Verification only — no private keys are handled here. Checked against the
official BIP-340 test vectors in relay/tests/test_bip340.py.
"""
from __future__ import annotations

import hashlib
from typing import Optional, Tuple

P = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F
N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
G = (
    0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798,
    0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8,
)

Point = Optional[Tuple[int, int]]


def tagged_hash(tag: str, msg: bytes) -> bytes:
    th = hashlib.sha256(tag.encode()).digest()
    return hashlib.sha256(th + th + msg).digest()


def point_add(p1: Point, p2: Point) -> Point:
    if p1 is None:
        return p2
    if p2 is None:
        return p1
    if p1[0] == p2[0] and p1[1] != p2[1]:
        return None
    if p1 == p2:
        lam = (3 * p1[0] * p1[0] * pow(2 * p1[1], P - 2, P)) % P
    else:
        lam = ((p2[1] - p1[1]) * pow(p2[0] - p1[0], P - 2, P)) % P
    x3 = (lam * lam - p1[0] - p2[0]) % P
    return (x3, (lam * (p1[0] - x3) - p1[1]) % P)


def point_mul(p: Point, n: int) -> Point:
    r: Point = None
    for i in range(256):
        if (n >> i) & 1:
            r = point_add(r, p)
        p = point_add(p, p)
    return r


def lift_x(x: int) -> Point:
    if x >= P:
        return None
    y_sq = (pow(x, 3, P) + 7) % P
    y = pow(y_sq, (P + 1) // 4, P)
    if pow(y, 2, P) != y_sq:
        return None
    return (x, y if y & 1 == 0 else P - y)


def verify(pubkey: bytes, msg: bytes, sig: bytes) -> bool:
    """BIP-340 verify: 32-byte x-only pubkey, message bytes, 64-byte sig."""
    if len(pubkey) != 32 or len(sig) != 64:
        return False
    pt = lift_x(int.from_bytes(pubkey, "big"))
    if pt is None:
        return False
    r = int.from_bytes(sig[:32], "big")
    s = int.from_bytes(sig[32:], "big")
    if r >= P or s >= N:
        return False
    e = int.from_bytes(
        tagged_hash("BIP0340/challenge", sig[:32] + pubkey + msg), "big") % N
    rpt = point_add(point_mul(G, s), point_mul(pt, N - e))
    if rpt is None or rpt[1] & 1 != 0 or rpt[0] != r:
        return False
    return True


def verify_hex(pubkey_hex: str, msg_hex: str, sig_hex: str) -> bool:
    """Hex wrapper; any malformed input is simply not a valid signature."""
    try:
        return verify(bytes.fromhex(pubkey_hex), bytes.fromhex(msg_hex),
                      bytes.fromhex(sig_hex))
    except (ValueError, TypeError):
        return False
