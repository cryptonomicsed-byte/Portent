"""BIP-340 verifier against the official vectors (P3-04 relies on it)."""
import pytest

from relay import bip340
from relay.tests.bip340_vectors import VECTORS
from relay.tests.nostr_fixtures import sign


@pytest.mark.parametrize("idx,sk,pk,aux,msg,sig,expected", VECTORS)
def test_official_vector_verify(idx, sk, pk, aux, msg, sig, expected):
    assert bip340.verify_hex(pk, msg, sig) is expected


@pytest.mark.parametrize("idx,sk,pk,aux,msg,sig,expected",
                         [v for v in VECTORS if v[1]])
def test_official_vector_sign_fixture(idx, sk, pk, aux, msg, sig, expected):
    # The test signer must reproduce the published signatures exactly.
    assert sign(int(sk, 16), bytes.fromhex(msg), bytes.fromhex(aux)).hex().upper() == sig


def test_malformed_inputs_are_invalid():
    assert bip340.verify_hex("zz", "00" * 32, "00" * 64) is False
    assert bip340.verify_hex("00" * 31, "00" * 32, "00" * 64) is False
    assert bip340.verify_hex("00" * 32, "00" * 32, "") is False
