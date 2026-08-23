#!/usr/bin/env python3
"""
Portent dashboard — minimal zero-dependency HTTP UI (humans are the exception;
agents consume the relay API directly).

Reads the same SQLite DB as the relay ($PORTENT_DB or ~/.portent/relay.db).
Dark aurora/glass aesthetic per the 2026 UI chart. Stdlib only.

  python3 dashboard/dashboard.py            # http://127.0.0.1:8898
  PORTENT_DASH_PORT=8898 PORTENT_DB=... python3 dashboard/dashboard.py
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DB_PATH = os.environ.get("PORTENT_DB",
                         os.path.expanduser("~/.portent/relay.db"))
DASH_PORT = int(os.environ.get("PORTENT_DASH_PORT", "8898"))

PAGE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PORTENT — prediction economy</title>
<style>
  :root{--bg:#070a14;--fg:#dbe4ff;--dim:#8b97b8;--acc:#7c5cff;--acc2:#00e5a0;
        --glass:rgba(255,255,255,.045);--line:rgba(255,255,255,.09);
        --warn:#ffb454;--bad:#ff5c7a;--good:var(--acc2);}
  *{box-sizing:border-box;margin:0;padding:0}
  body{background:radial-gradient(1200px 600px at 80% -10%,rgba(124,92,255,.22),transparent 60%),
       radial-gradient(900px 500px at 10% 110%,rgba(0,229,160,.14),transparent 60%),var(--bg);
       color:var(--fg);font:14px/1.55 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
       min-height:100vh;padding:32px 5vw}
  h1{font-size:26px;letter-spacing:.14em;text-transform:uppercase;
     background:linear-gradient(90deg,#b9a8ff,#00e5a0);-webkit-background-clip:text;
     background-clip:text;color:transparent;margin-bottom:4px}
  .sub{color:var(--dim);margin-bottom:24px;font-size:12px}
  nav a{color:var(--acc2);text-decoration:none;margin-right:18px;font-size:12px;
        letter-spacing:.08em;text-transform:uppercase}
  nav a:hover{text-decoration:underline}
  .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));
        gap:14px;margin:22px 0}
  .card{background:var(--glass);border:1px solid var(--line);border-radius:14px;
        padding:16px;backdrop-filter:blur(8px)}
  .card .k{color:var(--dim);font-size:11px;letter-spacing:.1em;
           text-transform:uppercase;margin-bottom:8px}
  .card .v{font-size:20px;font-weight:700}
  .card .v.good{color:var(--good)} .card .v.bad{color:var(--bad)}
  .card .v.warn{color:var(--warn)}
  table{width:100%;border-collapse:collapse;margin-top:14px;font-size:13px}
  th{color:var(--dim);text-align:left;font-weight:400;letter-spacing:.08em;
     text-transform:uppercase;font-size:11px;padding:8px 10px;
     border-bottom:1px solid var(--line)}
  td{padding:8px 10px;border-bottom:1px solid rgba(255,255,255,.05)}
  tr:hover td{background:rgba(124,92,255,.07)}
  .tier{display:inline-block;padding:2px 9px;border-radius:99px;font-size:11px;
        border:1px solid var(--line)}
  .tier.legendary{color:#ffd700;border-color:rgba(255,215,0,.4)}
  .tier.elite{color:#c792ff;border-color:rgba(199,146,255,.4)}
  .tier.trusted{color:#7cc4ff;border-color:rgba(124,196,255,.4)}
  .tier.verified{color:#00e5a0;border-color:rgba(0,229,160,.4)}
  .h2{margin-top:30px;font-size:15px;letter-spacing:.1em;text-transform:uppercase;
      color:var(--acc2)}
  .pill{display:inline-block;padding:2px 8px;border-radius:99px;font-size:11px}
  .pill.open{background:rgba(0,229,160,.12);color:var(--good)}
  .pill.resolved{background:rgba(124,196,255,.12);color:#7cc4ff}
  .pill.expired{background:rgba(255,92,122,.12);color:var(--bad)}
  .pill.disputed{background:rgba(255,180,84,.12);color:var(--warn)}
  footer{margin-top:40px;color:var(--dim);font-size:11px}
</style></head><body>
<h1>PORTENT</h1>
<div class="sub">nostr-native prediction economy · PORT · buyback + burn + staking + governance</div>
<nav><a href="/">overview</a><a href="/leaderboard">leaderboard</a>
<a href="/predictions">predictions</a><a href="/governance">governance</a>
<a href="/oracles">oracles</a></nav>
{CONTENT}
<footer>portent · nostr kinds 30007–30017 · buzz-compatible · keyed by pubkey</footer>
</body></html>"""


def conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def _fmt_tokens(raw: str) -> str:
    try:
        return f"{int(raw):,}"
    except (TypeError, ValueError):
        return str(raw)


def _fmt_sol(raw: str) -> str:
    try:
        return f"{float(raw):.2f}"
    except (TypeError, ValueError):
        return str(raw)


def tier_class(t: str) -> str:
    return f"tier {t}" if t in ("legendary", "elite", "trusted", "verified") \
        else "tier"


def overview(c: sqlite3.Connection) -> str:
    stats = {r["k"]: r["v"] for r in c.execute("SELECT k,v FROM stats")}
    open_n = c.execute("SELECT COUNT(*) n FROM predictions WHERE status='open'"
                       ).fetchone()["n"]
    resolved_n = stats.get("resolved_events", "0")
    oracle_n = c.execute("SELECT COUNT(*) n FROM oracles").fetchone()["n"]
    cards = [
        ("supply", "1,000,000,000 PORT", ""),
        ("burned", _fmt_tokens(stats.get("total_burned", "0")), "bad"),
        ("burn events", _fmt_tokens(stats.get("events_burned", "0")), ""),
        ("buyback reserve", _fmt_sol(stats.get("buyback_reserve_sol", "0")) +
         " SOL / " + _fmt_tokens(stats.get("buyback_reserve_tokens", "0")) +
         " PORT", "good"),
        ("staker pool", _fmt_tokens(stats.get("staker_pool_tokens", "0")), ""),
        ("settlements", _fmt_tokens(stats.get("settlements", "0")), ""),
        ("open markets", str(open_n), "warn"),
        ("resolved events", _fmt_tokens(resolved_n), "good"),
        ("oracles", str(oracle_n), ""),
    ]
    cards_html = "".join(
        f'<div class="card"><div class="k">{k}</div>'
        f'<div class="v {cls}">{v}</div></div>'
        for k, v, cls in cards)
    return f'<div class="grid">{cards_html}</div>'


def leaderboard(c: sqlite3.Connection) -> str:
    rows = c.execute("SELECT * FROM reputation WHERE resolved>0 "
                     "ORDER BY composite DESC, hit_rate_bp DESC LIMIT 25"
                     ).fetchall()
    body = "".join(
        f"<tr><td>{i+1}</td><td>{r['pubkey'][:12]}…</td>"
        f"<td>{r['resolved']}</td><td>{r['hit_rate_bp']/100:.1f}%</td>"
        f"<td>{r['accuracy']:.2f}</td><td>{r['composite']}</td>"
        f'<td><span class="{tier_class(r["tier"])}">{r["tier"]}</span></td>'
        f"</tr>" for i, r in enumerate(rows)) or "<tr><td colspan=7>no data</td></tr>"
    return ('<div class="h2">top predictors — accuracy-ranked</div>'
            "<table><tr><th>#</th><th>pubkey</th><th>resolved</th>"
            "<th>hit rate</th><th>accuracy</th><th>composite</th>"
            f"<th>tier</th></tr>{body}</table>")


def predictions(c: sqlite3.Connection) -> str:
    status = "open"
    rows = c.execute("SELECT * FROM predictions ORDER BY created_at DESC "
                     "LIMIT 150").fetchall()
    body = "".join(
        f"<tr><td>{r['market_id'][:16]}…</td><td>{r['predictor'][:12]}…</td>"
        f"<td>{r['side']}</td><td>{r['stake']:,}</td>"
        f"<td>{(r['odds_bp']/10000):.2f}x</td>"
        f"<td>{time.strftime('%m-%d %H:%M', time.localtime(r['deadline']))}</td>"
        f'<td><span class="pill {r["status"]}">{r["status"]}</span></td>'
        f"</tr>" for r in rows) or "<tr><td colspan=7>no predictions</td></tr>"
    return ('<div class="h2">predictions</div>'
            "<table><tr><th>market</th><th>predictor</th><th>side</th>"
            "<th>stake</th><th>odds</th><th>deadline</th><th>status</th></tr>"
            f"{body}</table>")


def governance(c: sqlite3.Connection) -> str:
    rows = c.execute("SELECT * FROM governance ORDER BY created_at DESC "
                     "LIMIT 25").fetchall()
    body = "".join(
        f"<tr><td>{r['proposal_id'][:14]}…</td><td>{r['ptype']}</td>"
        f"<td>{r['title'][:44]}</td><td>{r['votes_for']}</td>"
        f"<td>{r['votes_against']}</td><td>{r['quorum_pct']}%</td>"
        f"<td>{r['status']}</td></tr>" for r in rows) \
        or "<tr><td colspan=7>no proposals</td></tr>"
    return ('<div class="h2">governance — token-weighted</div>'
            "<table><tr><th>id</th><th>type</th><th>title</th>"
            "<th>for</th><th>against</th><th>quorum</th><th>status</th></tr>"
            f"{body}</table>")


def oracles(c: sqlite3.Connection) -> str:
    rows = c.execute("SELECT * FROM oracles ORDER BY resolutions DESC "
                     "LIMIT 25").fetchall()
    body = "".join(
        f"<tr><td>{r['pubkey'][:12]}…</td><td>{r['resolutions']}</td>"
        f"<td>{r['accuracy']:.2f}</td><td>{r['bond']:,}</td>"
        f"<td>{'active' if r['active'] else 'slashed'}</td></tr>"
        for r in rows) or "<tr><td colspan=5>no oracles</td></tr>"
    return ('<div class="h2">oracle panel</div>'
            "<table><tr><th>pubkey</th><th>resolutions</th><th>accuracy</th>"
            f"<th>bond</th><th>status</th></tr>{body}</table>")


ROUTES = {
    "/": overview,
    "/leaderboard": leaderboard,
    "/predictions": predictions,
    "/governance": governance,
    "/oracles": oracles,
}


class Handler(BaseHTTPRequestHandler):
    server_version = "PortentDashboard/1.0"

    def log_message(self, fmt, *args):
        sys.stderr.write("[dash] %s\n" % (fmt % args))

    def do_GET(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        c = conn()
        try:
            fn = ROUTES.get(path)
            content = fn(c) if fn else '<div class="h2">404</div>'
        except sqlite3.Error as exc:
            content = f'<div class="h2">db error: {exc}</div>'
        finally:
            c.close()
        html = PAGE.replace("{CONTENT}", content).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(html)))
        self.end_headers()
        self.wfile.write(html)


def main() -> None:
    srv = ThreadingHTTPServer(("127.0.0.1", DASH_PORT), Handler)
    print(f"[portent-dashboard] http://127.0.0.1:{DASH_PORT} db={DB_PATH}",
          flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n[portent-dashboard] shutdown")


if __name__ == "__main__":
    main()
