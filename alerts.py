"""
Outsized-move alerter -> Microsoft Teams (Power Automate 'Workflows' webhook).

Reads watchlist.csv, recomputes the same 1D/1W/1M/3M/1Yr moves as the dashboard,
and posts an alert to Teams when any name moves beyond its threshold. De-dupes by
(ticker, period, last-bar-date) so each move alerts once per trading day — and never
re-alerts on weekends/holidays (no new bar => no new alert).

Setup:
  1. Create a Teams webhook (see README_ALERTS.txt) and paste its URL into alerts_config.json
  2. Test:  python alerts.py --test     (sends a hello message to the channel)
  3. Dry:   python alerts.py --dry      (prints what WOULD alert — no send, state untouched)
  4. Live:  python alerts.py            (run on a schedule via setup_alerts_schedule.ps1)

Per-name thresholds: optional watchlist column `alert_thr` overrides the 1D default for that name
(e.g. 3 for Benchmarks indices, 8 for habitually volatile small caps). Optional `dashboard_url`
(env DASHBOARD_URL or config) appends a dashboard link to each alert.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pandas as pd
import yfinance as yf

try:                                   # Windows console is cp1252 — --dry prints 🟢/🔴 safely
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BASE = Path(__file__).parent
WATCHLIST = BASE / "watchlist.csv"
CONFIG = BASE / "alerts_config.json"
STATE = BASE / "alerts_state.json"

TODAY = pd.Timestamp(pd.Timestamp.now()).normalize()
MOVE_THRESH = {"1D": 5}   # alert on 1D moves only. To also alert on longer horizons,
# re-add e.g. "1W": 10, "1M": 20, "3M": 30, "1Yr": 50 (the dashboard still shows all of them).


def load_config(require_webhook=True):
    # Webhook URL: env var first (cloud / GitHub Actions secret), then local config file.
    cfg = {}
    if CONFIG.exists():
        try:
            cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
        except Exception:
            cfg = {}
    url = os.environ.get("TEAMS_WEBHOOK_URL", "").strip() or cfg.get("teams_webhook_url", "")
    if not url.startswith("http") and require_webhook:
        sys.exit("No webhook URL — set env TEAMS_WEBHOOK_URL or 'teams_webhook_url' in alerts_config.json.")
    cfg["teams_webhook_url"] = url
    # optional: a link back to the dashboard, appended to each alert (env or config; not a secret)
    cfg["dashboard_url"] = os.environ.get("DASHBOARD_URL", "").strip() or cfg.get("dashboard_url", "")
    return cfg


def load_state():
    try:
        return set(json.loads(STATE.read_text(encoding="utf-8")))
    except Exception:
        return set()


def save_state(keys):
    # keep ~30 days so the file doesn't grow forever (keys end with the bar date)
    cutoff = (TODAY - pd.Timedelta(days=30)).date().isoformat()
    keys = {k for k in keys if k.rsplit("|", 1)[-1] >= cutoff}
    STATE.write_text(json.dumps(sorted(keys)), encoding="utf-8")


def pct(now, then):
    if then in (None, 0) or now is None or pd.isna(then) or pd.isna(now):
        return None
    return (now / then - 1.0) * 100.0


def ret_asof(s, last_px, target):
    idx = s.index.tz_localize(None) if s.index.tz is not None else s.index
    if len(idx) == 0 or idx[0] > target:
        return None
    after = s[idx >= target]
    return pct(last_px, float(after.iloc[0])) if len(after) else None


def moves(s):
    """Same method as the dashboard. Returns (dict of moves, last-bar-date str)."""
    n = len(s)
    last = float(s.iloc[-1])
    idx = s.index
    ldn = idx[-1].tz_localize(None) if idx.tz is not None else idx[-1]
    m = {
        "1D": pct(last, float(s.iloc[-2]) if n >= 2 else None),
        "1W": pct(last, float(s.iloc[-5]) if n >= 5 else None),
        "1M": ret_asof(s, last, ldn - pd.DateOffset(months=1)),
        "3M": ret_asof(s, last, ldn - pd.DateOffset(months=3)),
        "1Yr": ret_asof(s, last, TODAY - pd.Timedelta(weeks=52)),
    }
    return m, ldn.date().isoformat()


def send_teams(url, title, lines):
    # Plain-text/HTML payload for a Power Automate "Post message in a chat or channel" flow
    # whose Message field = the expression  triggerBody()?['text'].  Teams renders basic HTML
    # (<b>, <br>), so the alert shows as a normal channel message instead of an adaptive card.
    head = f"<b>{title}</b><br><br>" if title else ""
    text = head + "<br>".join(lines)
    data = json.dumps({"text": text}).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read().decode("utf-8", "ignore")[:200]
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "ignore")[:300]
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


def main():
    dry = "--dry" in sys.argv            # compute + print, never send, never touch state
    cfg = load_config(require_webhook=not dry)
    url = cfg["teams_webhook_url"]
    dash = cfg.get("dashboard_url", "")
    thr = {**MOVE_THRESH, **cfg.get("thresholds", {})}

    if "--test" in sys.argv:
        msgs = [
            ("✅ Coverage Monitor — alerts connected",
             ["This is a test. Outsized-move alerts will arrive here.",
              "Thresholds: " + " · ".join(f"{p} ≥{v}%" for p, v in thr.items())]),
            ("",   # a sample in the real alert format, sent as its own message (shows one-by-one)
             ["🟢 Ticker: 9866 HK Equity (Nio)", "Stock: +6.35%", "Price: 45.20 (prev 42.50)", "— sample format —"]),
        ]
        for i, (title, lines) in enumerate(msgs):
            status, body = send_teams(url, title, lines)
            print(f"Test send -> HTTP {status}. {body}")
            if i < len(msgs) - 1:
                time.sleep(1.5)
        return

    seed = "--seed" in sys.argv   # record current movers WITHOUT sending (clean first run)
    # No day/hours gate — runs 24/7 so moves in ANY market are caught, including US-listed
    # ADRs (KWEB/BABA/JD/PDD/...) that trade overnight HKT, outside Asian hours. De-dupe by
    # (ticker|period|bar-date) means one alert per move, so closed-market runs send nothing.

    wl = pd.read_csv(WATCHLIST, dtype=str, encoding="utf-8-sig").fillna("")
    tickers = wl["ticker"].tolist()
    hist = yf.download(tickers, period="2y", interval="1d", auto_adjust=False,
                       group_by="ticker", threads=True, progress=False)

    state = load_state()
    movers, new_keys = [], []     # movers: list of (state_key, [lines]) — one Teams message each
    for _, r in wl.iterrows():
        t = r["ticker"]
        try:
            s = hist[t]["Close"].dropna() if len(tickers) > 1 else hist["Close"].dropna()
        except Exception:
            s = None
        if s is None or len(s) == 0:           # self-heal: retry a ticker the batch dropped
            try:
                s = yf.Ticker(t).history(period="2y", interval="1d", auto_adjust=False)["Close"].dropna()
            except Exception:
                s = None
        if s is None or len(s) < 2:
            continue
        m, bar = moves(s)
        thr_row = dict(thr)                                 # per-name 1D override from watchlist alert_thr
        try:                                                # (blank/invalid -> default; e.g. 3 for indices,
            ov = float(str(r.get("alert_thr", "")).strip())  # 8 for habitually volatile names)
            if ov > 0:
                thr_row["1D"] = ov
        except (TypeError, ValueError):
            pass
        hits = [(p, v) for p, v in m.items()
                if p in thr_row and v is not None and abs(v) >= thr_row[p] and f"{t}|{p}|{bar}" not in state]
        if not hits:
            continue
        last, prev = float(s.iloc[-1]), float(s.iloc[-2])   # prev = prior close (the 1D reference)
        bbg = str(r.get("bbg", "")).strip()
        # "9866 HK Equity"; indices keep their own suffix ("HSI Index", not "HSI Index Equity")
        label = bbg if bbg.endswith("Index") else (f"{bbg} Equity" if bbg else t)
        for p, v in hits:
            key = f"{t}|{p}|{bar}"
            new_keys.append(key)
            dot = "🟢" if v > 0 else ("🔴" if v < 0 else "⚪")
            lines = [
                f"{dot} Ticker: {label} ({r['name']})",
                f"Stock: {v:+.2f}%",
                f"Price: {last:,.2f} (prev {prev:,.2f})",
            ]
            if dash:
                lines.append(f'<a href="{dash}">📊 Dashboard</a>')
            movers.append((key, lines))

    if dry:
        print(f"[dry] {len(movers)} alert(s) would send (nothing sent, state untouched):")
        for key, lines in movers:
            print("  " + key + "  ->  " + " | ".join(lines))
        return

    if seed:
        save_state(state | set(new_keys))
        print(f"Seeded {len(new_keys)} current moves — future runs will alert only NEW moves.")
        return

    if not movers:
        print("No new outsized moves.")
        return

    # Fire one message per mover (paced so Power Automate / Teams don't throttle a burst).
    sent_keys, sent = [], 0
    for i, (key, lines) in enumerate(movers):
        status, body = send_teams(url, "", lines)           # no title -> bare 3-line message
        if status in (200, 202):
            sent += 1; sent_keys.append(key)
        else:
            print(f"Send FAILED for {key} (HTTP {status}): {body}")
        if i < len(movers) - 1:
            time.sleep(1.5)
    if sent_keys:
        save_state(state | set(sent_keys))                  # only mark as seen what actually sent
    print(f"Sent {sent}/{len(movers)} message(s).")


if __name__ == "__main__":
    main()
