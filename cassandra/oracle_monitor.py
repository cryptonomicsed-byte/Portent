"""Oracle monitor (from Cassandra's monitoring/oracle-monitor.ts).

The original's metrics were all Math.random(): CPU, latency, service health
and even precision/recall. This keeps its useful parts -- the bounded metrics
history and the anomaly rules -- and feeds them only from real probes:

  - latency/error rate: timed HTTP GETs of configured health URLs
    (PORTENT_ORACLE_MONITOR_URLS="name=url,name=url"), stdlib only
  - CPU/memory: /proc/loadavg and /proc/meminfo where they exist (else None)
  - accuracy: passed in from a CommitLedger, never invented

A metric that can't be measured is None and never trips an anomaly.
Flag PORTENT_ORACLE_MONITOR_ENABLED (default off) gates probe(); the pure
functions (detect_anomalies, MetricsHistory) are always usable.
"""
from __future__ import annotations

import os
import time
import urllib.request
from collections import deque
from dataclasses import asdict, dataclass, field
from typing import Callable, Dict, List, Optional


def enabled() -> bool:
    return os.environ.get("PORTENT_ORACLE_MONITOR_ENABLED", "0").strip().lower() in ("1", "true", "yes", "on")


@dataclass
class SystemMetrics:
    timestamp: float
    cpu_usage: Optional[float] = None       # % (1-min loadavg / cores)
    memory_usage: Optional[float] = None    # %
    error_rate: Optional[float] = None      # fraction of probes failed
    latency_p50: Optional[float] = None     # ms
    latency_p99: Optional[float] = None     # ms
    service_health: Dict[str, bool] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


def detect_anomalies(m: SystemMetrics) -> List[str]:
    """Cassandra's rules, unchanged thresholds; unmeasured metrics are skipped.
    (error_rate is a fraction here; the original compared a 0-0.5 random
    number to 1, so >0.5 failing probes is the rule that can actually fire.)"""
    out = []
    if m.cpu_usage is not None and m.cpu_usage > 90:
        out.append("High CPU usage detected")
    if m.memory_usage is not None and m.memory_usage > 85:
        out.append("High memory usage detected")
    if m.error_rate is not None and m.error_rate > 0.5:
        out.append("Elevated error rate detected")
    if m.latency_p99 is not None and m.latency_p99 > 1000:
        out.append("High latency detected")
    if m.service_health:
        healthy = sum(1 for v in m.service_health.values() if v)
        if healthy < len(m.service_health) * 0.8:
            out.append("Multiple service health issues")
    return out


class MetricsHistory:
    def __init__(self, maxlen: int = 1000) -> None:
        self._h: deque = deque(maxlen=maxlen)

    def add(self, m: SystemMetrics) -> None:
        self._h.append(m)

    def latest(self) -> Optional[SystemMetrics]:
        return self._h[-1] if self._h else None

    def all(self) -> List[SystemMetrics]:
        return list(self._h)


def _pct(vals: List[float], q: float) -> Optional[float]:
    if not vals:
        return None
    s = sorted(vals)
    return s[min(len(s) - 1, int(round(q * (len(s) - 1))))]


def _cpu() -> Optional[float]:
    try:
        with open("/proc/loadavg") as f:
            load1 = float(f.read().split()[0])
        return round(100.0 * load1 / (os.cpu_count() or 1), 1)
    except (OSError, ValueError):
        return None


def _mem() -> Optional[float]:
    try:
        info = {}
        with open("/proc/meminfo") as f:
            for line in f:
                k, v = line.split(":", 1)
                info[k] = float(v.split()[0])
        return round(100.0 * (1 - info["MemAvailable"] / info["MemTotal"]), 1)
    except (OSError, KeyError, ValueError, ZeroDivisionError):
        return None


def parse_urls(spec: Optional[str] = None) -> Dict[str, str]:
    spec = spec if spec is not None else os.environ.get("PORTENT_ORACLE_MONITOR_URLS", "")
    out = {}
    for part in spec.split(","):
        if "=" in part:
            k, v = part.split("=", 1)
            if k.strip() and v.strip():
                out[k.strip()] = v.strip()
    return out


def _http_ok(url: str, timeout: float) -> bool:
    with urllib.request.urlopen(url, timeout=timeout) as r:  # noqa: S310 -- operator-configured URL
        return 200 <= r.status < 400


def probe(urls: Optional[Dict[str, str]] = None, timeout: float = 5.0,
          getter: Callable[[str, float], bool] = _http_ok) -> Optional[SystemMetrics]:
    """One real measurement, or None when the flag is off."""
    if not enabled():
        return None
    urls = urls if urls is not None else parse_urls()
    lat, health = [], {}
    for name, url in urls.items():
        t0 = time.monotonic()
        try:
            ok = bool(getter(url, timeout))
        except Exception:  # noqa: BLE001 -- a failed probe is a data point
            ok = False
        health[name] = ok
        if ok:
            lat.append((time.monotonic() - t0) * 1000.0)
    return SystemMetrics(timestamp=time.time(), cpu_usage=_cpu(), memory_usage=_mem(),
                         error_rate=(1 - sum(health.values()) / len(health)) if health else None,
                         latency_p50=_pct(lat, 0.5), latency_p99=_pct(lat, 0.99), service_health=health)
