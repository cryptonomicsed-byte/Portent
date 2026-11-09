"""Vector client (from Cassandra's core/vectordb-client.ts).

Stores predictions in Qdrant with Ollama embeddings and finds similar ones.
The original fell back to a RANDOM embedding when Ollama failed, which made
"similar patterns" meaningless; here an embedding failure raises
EmbeddingUnavailable and nothing is stored or returned.

Stdlib HTTP only (Qdrant REST + Ollama /api/embed). Flag
PORTENT_VECTOR_ENABLED (default off): with it off every call is a no-op that
returns None/[] -- Portent never talks to either service by default.
Env: QDRANT_URL (http://localhost:6333), QDRANT_COLLECTION
(cassandra_predictions), OLLAMA_BASE_URL (http://localhost:11434),
CASSANDRA_EMBED_MODEL (nomic-embed-text).
"""
from __future__ import annotations

import hashlib
import json
import os
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, List, Optional

from .types import ParadigmPrediction


class EmbeddingUnavailable(RuntimeError):
    pass


def enabled() -> bool:
    return os.environ.get("PORTENT_VECTOR_ENABLED", "0").strip().lower() in ("1", "true", "yes", "on")


def _http(method: str, url: str, body: Optional[dict] = None, timeout: float = 15.0) -> Dict[str, Any]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 -- operator-configured URL
        raw = r.read()
    return json.loads(raw) if raw else {}


def point_id(prediction_id: str) -> str:
    """Qdrant wants an unsigned int or UUID; derive a stable UUID."""
    h = hashlib.sha256(prediction_id.encode()).hexdigest()
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


class VectorClient:
    def __init__(self, http: Callable[..., Dict[str, Any]] = _http) -> None:
        self.http = http
        self.qdrant = os.environ.get("QDRANT_URL", "http://localhost:6333").rstrip("/")
        self.collection = os.environ.get("QDRANT_COLLECTION", "cassandra_predictions")
        self.ollama = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/")
        self.model = os.environ.get("CASSANDRA_EMBED_MODEL", "nomic-embed-text")
        self._dim: Optional[int] = None

    def embed(self, text: str) -> List[float]:
        try:
            out = self.http("POST", f"{self.ollama}/api/embed", {"model": self.model, "input": text})
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise EmbeddingUnavailable(str(e)) from e
        vecs = out.get("embeddings") or []
        if not vecs or not vecs[0]:
            raise EmbeddingUnavailable("no embedding returned")
        return [float(x) for x in vecs[0]]

    def ensure_collection(self, dim: int) -> None:
        if self._dim == dim:
            return
        try:
            self.http("GET", f"{self.qdrant}/collections/{self.collection}")
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise
            self.http("PUT", f"{self.qdrant}/collections/{self.collection}",
                      {"vectors": {"size": dim, "distance": "Cosine"}})
        self._dim = dim

    def store(self, p: ParadigmPrediction) -> Optional[str]:
        if not enabled():
            return None
        vec = self.embed(p.description)
        self.ensure_collection(len(vec))
        pid = point_id(p.id)
        self.http("PUT", f"{self.qdrant}/collections/{self.collection}/points?wait=true",
                  {"points": [{"id": pid, "vector": vec, "payload": p.to_dict()}]})
        return pid

    def similar(self, description: str, limit: int = 5) -> List[Dict[str, Any]]:
        if not enabled():
            return []
        vec = self.embed(description)
        self.ensure_collection(len(vec))
        out = self.http("POST", f"{self.qdrant}/collections/{self.collection}/points/search",
                        {"vector": vec, "limit": int(limit), "with_payload": True})
        return [{"score": r.get("score"), "prediction": r.get("payload")} for r in out.get("result", [])]
