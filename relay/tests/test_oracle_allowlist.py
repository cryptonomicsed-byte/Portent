"""P1-14: settlement allowlist (interim, until P3-04 reads Crucible's list).

Acceptance: an unlisted key cannot settle; a listed key settles; unset keeps
today's behaviour. Plus: the unlisted resolution is kept and listed under
GET /resolutions?status=unverified, and never becomes an oracle.
"""
import importlib.util
import json
import pathlib
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

RELAY = pathlib.Path(__file__).resolve().parents[1] / "portent_relay.py"
LISTED, OTHER = "aa" * 32, "bb" * 32


@pytest.fixture
def relay(tmp_path, monkeypatch):
    monkeypatch.setenv("PORTENT_DB", str(tmp_path / "relay.db"))
    spec = importlib.util.spec_from_file_location("portent_relay_t", RELAY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _open_market(mod, market):
    c = mod._conn()
    mod._init_stats(c)
    now = int(time.time())
    c.execute("INSERT INTO predictions(market_id, event_id, predictor, side, stake, odds_bp, deadline, "
              "status, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
              (market, "ev-" + market, "p1", "yes", 1000, 5000, now + 3600, "open", now))
    c.commit()
    return c


def _resolve(mod, c, market, pubkey, outcome="yes"):
    ev = {"id": f"res-{market}-{pubkey[:4]}", "pubkey": pubkey, "kind": mod.KIND_RESOLUTION,
          "tags": [["m", market], ["outcome", outcome]], "content": json.dumps({"confidence": 0.9}),
          "created_at": int(time.time())}
    mod._store_event(c, ev)
    mod._handle_30016(c, ev)
    c.commit()


def _status(c, market):
    return c.execute("SELECT status FROM predictions WHERE market_id=?", (market,)).fetchone()[0]


def test_unlisted_key_cannot_settle_and_is_kept_as_unverified(relay, monkeypatch):
    monkeypatch.setenv("PORTENT_ORACLE_PUBKEYS", f"{LISTED.upper()}, ")
    c = _open_market(relay, "m-1")
    _resolve(relay, c, "m-1", OTHER)
    assert _status(c, "m-1") == "open"
    assert c.execute("SELECT COUNT(*) FROM resolutions").fetchone()[0] == 0
    assert c.execute("SELECT COUNT(*) FROM oracles WHERE pubkey=?", (OTHER,)).fetchone()[0] == 0
    row = c.execute("SELECT oracle, outcome FROM unverified_resolutions").fetchone()
    assert tuple(row) == (OTHER, "yes")
    assert c.execute("SELECT COUNT(*) FROM events WHERE pubkey=?", (OTHER,)).fetchone()[0] == 1


def test_listed_key_settles(relay, monkeypatch):
    monkeypatch.setenv("PORTENT_ORACLE_PUBKEYS", f"{LISTED},{'cc' * 32}")
    c = _open_market(relay, "m-2")
    _resolve(relay, c, "m-2", LISTED)
    assert _status(c, "m-2") != "open"
    assert c.execute("SELECT oracle FROM resolutions WHERE market_id='m-2'").fetchone()[0] == LISTED


def test_unset_keeps_todays_behaviour(relay, monkeypatch):
    monkeypatch.delenv("PORTENT_ORACLE_PUBKEYS", raising=False)
    c = _open_market(relay, "m-3")
    _resolve(relay, c, "m-3", OTHER)
    assert _status(c, "m-3") != "open"
    assert c.execute("SELECT COUNT(*) FROM unverified_resolutions").fetchone()[0] == 0


def test_get_resolutions_unverified(relay, monkeypatch):
    monkeypatch.setenv("PORTENT_ORACLE_PUBKEYS", LISTED)
    c = _open_market(relay, "m-4")
    _resolve(relay, c, "m-4", OTHER)
    c.close()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), relay.PortentRelayHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        un = json.loads(urllib.request.urlopen(base + "/resolutions?status=unverified").read())
        ok = json.loads(urllib.request.urlopen(base + "/resolutions").read())
    finally:
        srv.shutdown()
    assert un["status"] == "unverified" and [r["market_id"] for r in un["resolutions"]] == ["m-4"]
    assert ok == {"resolutions": []}  # today's shape, nothing settled
