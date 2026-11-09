"""P3-05: Cassandra parts in Portent -- types, commit-then-verify, monitor,
vector client. No Math.random engine, no code generator, nothing random."""
import json
import pathlib
import urllib.error

import pytest

from cassandra import commit_verify as cv
from cassandra import oracle_monitor as om
from cassandra import vector_client as vc
from cassandra.types import ParadigmPrediction, Signals, Timeline


def _pred(pid="p1", desc="WASM components replace containers", prob=0.7):
    return ParadigmPrediction(id=pid, paradigm_type="architectural", paradigm_name="wasm",
                              description=desc, probability=prob, confidence=0.85,
                              timeline=Timeline(100, 50, (90, 120)),
                              signals=Signals(github_trends=["wasmtime"]))


def test_types_validate_and_roundtrip():
    p = _pred()
    assert ParadigmPrediction.from_dict(json.loads(json.dumps(p.to_dict()))).to_dict() == p.to_dict()
    with pytest.raises(ValueError):
        _pred(prob=1.5)
    with pytest.raises(ValueError):
        Timeline(1, 1, (5, 2))


def test_commit_reveal_validate():
    led = cv.CommitLedger()
    p = _pred()
    proof, salt = led.commit(p, now=1000)
    assert "salt" not in json.dumps(proof.to_dict()) and salt not in json.dumps(proof.to_dict())
    with pytest.raises(ValueError):
        led.commit(p)  # once
    with pytest.raises(ValueError):
        led.validate("p1", True)  # reveal first
    assert led.reveal(proof, p, salt, now=2000)
    out = led.validate("p1", True, now=1000 + 86400 * 3)
    assert out["lead_time_s"] == 86400 * 3
    with pytest.raises(ValueError):
        led.validate("p1", False)  # one-shot
    acc = led.accuracy()
    assert acc["correct"] == 1 and acc["accuracy"] == 1.0 and acc["average_lead_time_days"] == 3.0


def test_tampering_fails_verification():
    led = cv.CommitLedger()
    p = _pred()
    proof, salt = led.commit(p)
    assert not led.reveal(proof, _pred(prob=0.71), salt)       # changed claim
    assert not led.reveal(proof, p, "00" * 32)                  # wrong salt
    p2 = _pred()
    p2.signals.github_trends.append("added later")
    assert not cv.CommitLedger.verify(proof, p2, salt)          # changed reasoning
    assert cv.CommitLedger.verify(proof, p, salt)


def test_commitment_is_deterministic_and_status_independent():
    p = _pred()
    h1 = cv.prediction_hash(p)
    p.status = "committed"
    assert cv.prediction_hash(p) == h1


def test_anchor_event_shape():
    proof, _ = cv.CommitLedger().commit(_pred())
    ev = cv.anchor_event(proof, "ab" * 32, now=5)
    assert ev["kind"] == 30009 and ["commitment", proof.commitment] in ev["tags"] and "sig" not in ev


def test_monitor_rules_and_none_never_trips():
    assert om.detect_anomalies(om.SystemMetrics(timestamp=0)) == []
    m = om.SystemMetrics(timestamp=0, cpu_usage=95, memory_usage=90, error_rate=0.6, latency_p99=1500,
                         service_health={"a": True, "b": False, "c": False})
    assert len(om.detect_anomalies(m)) == 5
    h = om.MetricsHistory(maxlen=2)
    for i in range(3):
        h.add(om.SystemMetrics(timestamp=i))
    assert [x.timestamp for x in h.all()] == [1, 2] and h.latest().timestamp == 2


def test_probe_off_by_default_and_real_when_on(monkeypatch):
    assert om.probe({"x": "http://nowhere"}) is None
    monkeypatch.setenv("PORTENT_ORACLE_MONITOR_ENABLED", "1")

    def getter(url, timeout):
        if "bad" in url:
            raise OSError("down")
        return True
    m = om.probe({"ok": "http://ok", "bad": "http://bad"}, getter=getter)
    assert m.service_health == {"ok": True, "bad": False} and m.error_rate == 0.5
    assert m.latency_p50 is not None
    assert om.parse_urls("a=http://x, b=http://y,junk") == {"a": "http://x", "b": "http://y"}


def test_vector_client_off_by_default():
    calls = []
    c = vc.VectorClient(http=lambda *a, **k: calls.append(a) or {})
    assert c.store(_pred()) is None and c.similar("x") == [] and calls == []


def test_vector_client_never_uses_a_random_embedding(monkeypatch):
    monkeypatch.setenv("PORTENT_VECTOR_ENABLED", "1")

    def down(method, url, body=None, timeout=15.0):
        raise urllib.error.URLError("ollama down")
    with pytest.raises(vc.EmbeddingUnavailable):
        vc.VectorClient(http=down).store(_pred())


def test_vector_client_store_and_search(monkeypatch):
    monkeypatch.setenv("PORTENT_VECTOR_ENABLED", "1")
    seen = []

    def http(method, url, body=None, timeout=15.0):
        seen.append((method, url.split("/", 3)[-1], body))
        if url.endswith("/api/embed"):
            return {"embeddings": [[0.1, 0.2, 0.3]]}
        if method == "GET":
            raise urllib.error.HTTPError(url, 404, "nf", {}, None)
        if url.endswith("/points/search"):
            return {"result": [{"score": 0.9, "payload": {"id": "p1"}}]}
        return {}
    c = vc.VectorClient(http=http)
    pid = c.store(_pred())
    assert pid == vc.point_id("p1") and len(pid) == 36
    assert any(m == "PUT" and b == {"vectors": {"size": 3, "distance": "Cosine"}} for m, _, b in seen)
    assert c.similar("wasm")[0]["score"] == 0.9


def test_dropped_parts_not_ported():
    root = pathlib.Path(__file__).resolve().parents[1]
    src = "\n".join(p.read_text() for p in (root / "cassandra").glob("*.py"))
    assert "import random" not in src and "random.random" not in src
    assert not (root / "cassandra" / "code_generator.py").exists()
    assert not (root / "cassandra" / "prediction_engine.py").exists()
