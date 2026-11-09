"""Cassandra parts merged into Portent (P3-05).

Ported from cryptonomicsed-byte/project-12-Cassandra-Oracle @199d6ed (TypeScript)
into stdlib Python:

  types.py          core data structures (prediction, commitment proof, metrics)
  commit_verify.py  commit-then-verify ledger (salted sha256 commitment, reveal,
                    one-shot validation) + optional Portent 30009 anchor event
  oracle_monitor.py metrics + anomaly detection from REAL probes (no random)
  vector_client.py  Qdrant + Ollama semantic store (no random embeddings)

Deliberately NOT ported: core/prediction-engine.ts (Math.random "engine"),
core/code-generator.ts (code generator), and the autonomous executor and
mock blockchain logger that sit on top of them.

Opt-in only: nothing in relay/, sdk/, agent/ or dashboard/ imports this
package, so Portent behaves exactly as before. Network-touching pieces have
their own flags (PORTENT_VECTOR_ENABLED, PORTENT_ORACLE_MONITOR_ENABLED),
default off.
"""
