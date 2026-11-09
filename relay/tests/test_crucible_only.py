"""P3-04: Portent settles only from Crucible keys.

Phase-1 done-when: "unlisted keys can't settle Portent" — tested exactly,
with real BIP-340-signed events, both at the handler and over HTTP.
"""
import importlib.util
import json
import pathlib
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from relay.tests.nostr_fixtures import event, xonly

RELAY = pathlib.Path(__file__).resolve().parents[1] / "portent_relay.py"
CRUCIBLE_SK, OUTSIDER_SK, PREDICTOR_SK = 0xC0FFEE, 0xBADBAD, 0x5EED  # test scalars
CRUCIBLE, OUTSIDER = xonly(CRUCIBLE_SK), xonly(OUTSIDER_SK)


@pytest.fixture
def relay(tmp_path, monkeypatch):
    monkeypatch.setenv("PORTENT_DB", str(tmp_path / "relay.db"))
    for k in ("PORTENT_CRUCIBLE_ONLY", "PORTENT_CRUCIBLE_KEYS", "PORTENT_CRUCIBLE_POLICY",
              "PORTENT_REQUIRE_SIG"):
        monkeypatch.delenv(k, raising=False)
    spec = importlib.util.spec_from_file_location("portent_relay_p304", RELAY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _open_market(mod, market):
    c = mod._conn()
    mod._init_stats(c)
    now = int(time.time())
    pred = event(PREDICTOR_SK, mod.KIND_PREDICTION_POST,
                 [["m", market], ["side", "yes"], ["stake", "1000"], ["deadline", str(now + 3600)]],
                 "", now)
    mod._store_event(c, pred)
    mod._handle_30015(c, pred)
    c.commit()
    return c


def _resolution(mod, sk, market, outcome="yes", sig_key=None):
    return event(sk, mod.KIND_RESOLUTION, [["m", market], ["outcome", outcome]],
                 json.dumps({"confidence": 0.9}), int(time.time()), sig_key=sig_key)


def _apply(mod, c, ev):
    mod._store_event(c, ev)
    mod._handle_30016(c, ev)
    c.commit()


def _status(c, market):
    return c.execute("SELECT status FROM predictions WHERE market_id=?", (market,)).fetchone()[0]


def _refusal(c, eid):
    row = c.execute("SELECT reason FROM crucible_refused_resolutions WHERE event_id=?",
                    (eid,)).fetchone()
    return row[0] if row else None


def _assert_not_settled(c, market, key):
    assert _status(c, market) == "open"
    assert c.execute("SELECT COUNT(*) FROM resolutions").fetchone()[0] == 0
    assert c.execute("SELECT COUNT(*) FROM oracles WHERE pubkey=?", (key,)).fetchone()[0] == 0
    assert c.execute("SELECT v FROM stats WHERE k='settlements'").fetchone()[0] == "0"


def test_unlisted_key_cannot_settle(relay, monkeypatch):
    monkeypatch.setenv("PORTENT_CRUCIBLE_ONLY", "1")
    monkeypatch.setenv("PORTENT_CRUCIBLE_KEYS", CRUCIBLE)
    c = _open_market(relay, "m-unlisted")
    ev = _resolution(relay, OUTSIDER_SK, "m-unlisted")  # validly signed by its own key
    _apply(relay, c, ev)
    _assert_not_settled(c, "m-unlisted", OUTSIDER)
    assert _refusal(c, ev["id"]) == "unlisted_key"
    # The event itself is still stored, as before.
    assert c.execute("SELECT COUNT(*) FROM events WHERE id=?", (ev["id"],)).fetchone()[0] == 1


def test_listed_crucible_key_settles(relay, monkeypatch):
    monkeypatch.setenv("PORTENT_CRUCIBLE_ONLY", "true")
    monkeypatch.setenv("PORTENT_CRUCIBLE_KEYS", f" {CRUCIBLE.upper()} ,{'ab' * 32}")
    c = _open_market(relay, "m-listed")
    _apply(relay, c, _resolution(relay, CRUCIBLE_SK, "m-listed"))
    assert _status(c, "m-listed") == "resolved"
    assert c.execute("SELECT oracle FROM resolutions WHERE market_id='m-listed'").fetchone()[0] == CRUCIBLE


def test_listed_pubkey_with_forged_signature_cannot_settle(relay, monkeypatch):
    monkeypatch.setenv("PORTENT_CRUCIBLE_ONLY", "1")
    monkeypatch.setenv("PORTENT_CRUCIBLE_KEYS", CRUCIBLE)
    c = _open_market(relay, "m-forged")
    ev = _resolution(relay, CRUCIBLE_SK, "m-forged", sig_key=OUTSIDER_SK)  # claims Crucible's key
    _apply(relay, c, ev)
    _assert_not_settled(c, "m-forged", CRUCIBLE)
    assert _refusal(c, ev["id"]) == "bad_signature"


def test_listed_pubkey_unsigned_cannot_settle_even_without_require_sig(relay, monkeypatch):
    monkeypatch.setenv("PORTENT_CRUCIBLE_ONLY", "1")
    monkeypatch.setenv("PORTENT_CRUCIBLE_KEYS", CRUCIBLE)
    c = _open_market(relay, "m-unsigned")
    ev = _resolution(relay, CRUCIBLE_SK, "m-unsigned")
    ev["sig"] = ""
    _apply(relay, c, ev)
    _assert_not_settled(c, "m-unsigned", CRUCIBLE)
    assert _refusal(c, ev["id"]) == "bad_signature"


def test_tampered_outcome_cannot_settle(relay, monkeypatch):
    monkeypatch.setenv("PORTENT_CRUCIBLE_ONLY", "1")
    monkeypatch.setenv("PORTENT_CRUCIBLE_KEYS", CRUCIBLE)
    c = _open_market(relay, "m-tamper")
    ev = _resolution(relay, CRUCIBLE_SK, "m-tamper", outcome="no")
    ev["tags"] = [["m", "m-tamper"], ["outcome", "yes"]]  # id/sig no longer cover this
    _apply(relay, c, ev)
    _assert_not_settled(c, "m-tamper", CRUCIBLE)
    assert _refusal(c, ev["id"]) == "bad_event_id"


def test_flag_on_with_empty_list_fails_closed(relay, monkeypatch):
    monkeypatch.setenv("PORTENT_CRUCIBLE_ONLY", "1")
    c = _open_market(relay, "m-empty")
    ev = _resolution(relay, CRUCIBLE_SK, "m-empty")
    _apply(relay, c, ev)
    _assert_not_settled(c, "m-empty", CRUCIBLE)
    assert _refusal(c, ev["id"]) == "no_crucible_keys_configured"


def test_policy_file_oracle_authorities(relay, monkeypatch, tmp_path):
    pol = tmp_path / "policy.json"
    pol.write_text(json.dumps({"allow_unrostered": True, "oracle_authorities": [CRUCIBLE]}))
    monkeypatch.setenv("PORTENT_CRUCIBLE_ONLY", "1")
    monkeypatch.setenv("PORTENT_CRUCIBLE_POLICY", str(pol))
    c = _open_market(relay, "m-pol-out")
    _apply(relay, c, _resolution(relay, OUTSIDER_SK, "m-pol-out"))
    assert _status(c, "m-pol-out") == "open"
    c2 = _open_market(relay, "m-pol-in")
    _apply(relay, c2, _resolution(relay, CRUCIBLE_SK, "m-pol-in"))
    assert _status(c2, "m-pol-in") == "resolved"


def test_unreadable_policy_file_fails_closed(relay, monkeypatch, tmp_path):
    bad = tmp_path / "policy.json"
    bad.write_text("{not json")
    monkeypatch.setenv("PORTENT_CRUCIBLE_ONLY", "1")
    monkeypatch.setenv("PORTENT_CRUCIBLE_POLICY", str(bad))
    c = _open_market(relay, "m-badpol")
    _apply(relay, c, _resolution(relay, CRUCIBLE_SK, "m-badpol"))
    assert _status(c, "m-badpol") == "open"


def test_flag_off_keeps_existing_behaviour(relay, monkeypatch):
    # Keys configured but flag off: any key still settles, gate never entered.
    monkeypatch.setenv("PORTENT_CRUCIBLE_KEYS", CRUCIBLE)
    called = []
    monkeypatch.setattr(relay._crucible_gate, "check_resolution",
                        lambda ev: called.append(ev) or (False, "x"))
    c = _open_market(relay, "m-off")
    ev = _resolution(relay, OUTSIDER_SK, "m-off")
    ev["sig"] = ""  # unsigned, as legacy deployments allow
    _apply(relay, c, ev)
    assert _status(c, "m-off") == "resolved"
    assert called == []
    assert c.execute("SELECT name FROM sqlite_master WHERE name='crucible_refused_resolutions'"
                     ).fetchone() is None


def test_unlisted_key_cannot_settle_over_http(relay, monkeypatch):
    monkeypatch.setenv("PORTENT_CRUCIBLE_ONLY", "1")
    monkeypatch.setenv("PORTENT_CRUCIBLE_KEYS", CRUCIBLE)
    monkeypatch.setattr(relay, "_notify_vantage_resolution", lambda *a, **k: None)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), relay.PortentRelayHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"

    def post(ev):
        req = urllib.request.Request(base + "/event", data=json.dumps({"event": ev}).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        return json.loads(urllib.request.urlopen(req).read())

    def get(path):
        return json.loads(urllib.request.urlopen(base + path).read())

    try:
        now = int(time.time())
        for m in ("h-1", "h-2"):
            post(event(PREDICTOR_SK, 30015, [["m", m], ["side", "yes"], ["deadline", str(now + 3600)]],
                       "", now))
        assert post(_resolution(relay, OUTSIDER_SK, "h-1"))["ok"] is True
        assert [p["status"] for p in get("/prediction/h-1")["predictions"]] == ["open"]
        assert post(_resolution(relay, CRUCIBLE_SK, "h-2"))["ok"] is True
        assert [p["status"] for p in get("/prediction/h-2")["predictions"]] == ["resolved"]
        assert [r["market_id"] for r in get("/resolutions")["resolutions"]] == ["h-2"]
        assert [o["pubkey"] for o in get("/oracles")["oracles"]] == [CRUCIBLE]
    finally:
        srv.shutdown()
