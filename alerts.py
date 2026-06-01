"""
Outsized-move alerter -> Microsoft Teams (Power Automate 'Workflows' webhook).

Reads watchlist.csv, recomputes the same 1D/1W/1M/3M/1Yr moves as the dashboard,
and posts an alert to Teams when any name moves beyond its threshold. De-dupes by
(ticker, period, last-bar-date) so each move alerts once per trading day — and never
re-alerts on weekends/holidays (no new bar => no new alert).

Setup:
  1. Create a Teams webhook (see README_ALERTS.txt) and paste its URL into alerts_config.json
  2. Test:  python alerts.py --test     (sends a hello message to the channel)
  3. Live:  python alerts.py            (run on a schedule via setup_alerts_schedule.ps1)
"""
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pandas as pd
import yfinance as yf

BASE = Path(__file__).parent
WATCHLIST = BASE / "watchlist.csv"
CONFIG = BASE / "alerts_config.json"
STATE = BASE / "alerts_state.json"

TODAY = pd.Timestamp(pd.Timestamp.now()).normalize()
MOVE_THRESH = {"1D": 5, "1W": 10, "1M": 20, "3M": 30, "1Yr": 50}


def load_config():
    # Webhook URL: env var first (cloud / GitHub Actions secret), then local config file.
    cfg = {}
    if CONFIG.exists():
        try:
            cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
        except Exception:
            cfg = {}
    url = os.environ.get("TEAMS_WEBHOOK_URL", "").strip() or cfg.get("teams_webhook_url", "")
    if not url.startswith("http"):
        sys.exit("No webhook URL — set env TEAMS_WEBHOOK_URL or 'teams_webhook_url' in alerts_config.json.")
    cfg["teams_webhook_url"] = url
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
    card = {
        "type": "message",
        "attachments": [{
            "contentType": "application/vnd.microsoft.card.adaptive",
            "content": {
                "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                "type": "AdaptiveCard",
                "version": "1.4",
                "body": [
                    {"type": "TextBlock", "text": title, "weight": "Bolder", "size": "Medium", "wrap": True},
                    {"type": "TextBlock", "text": "\n\n".join(lines), "wrap": True},
                ],
            },
        }],
    }
    data = json.dumps(card).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read().decode("utf-8", "ignore")[:200]
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "ignore")[:300]
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


def main():
    cfg = load_config()
    url = cfg["teams_webhook_url"]
    thr = {**MOVE_THRESH, **cfg.get("thresholds", {})}

    if "--test" in sys.argv:
        status, body = send_teams(url, "✅ Coverage Monitor — alerts connected",
                                  ["This is a test. Outsized-move alerts will arrive here.",
                                   f"Thresholds: 1D ≥{thr['1D']}% · 1W ≥{thr['1W']}% · 1M ≥{thr['1M']}% · 3M ≥{thr['3M']}% · 1Yr ≥{thr['1Yr']}%"])
        print(f"Test send -> HTTP {status}. {body}")
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
    alerts, new_keys = [], []
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
        hits = [(p, v) for p, v in m.items() if v is not None and abs(v) >= thr[p]]
        hits = [(p, v) for p, v in hits if f"{t}|{p}|{bar}" not in state]
        if not hits:
            continue
        for p, _ in hits:
            new_keys.append(f"{t}|{p}|{bar}")
        signs = {("+" if v > 0 else "-") for _, v in hits}
        dot = "🟢" if signs == {"+"} else ("🔴" if signs == {"-"} else "🔵")
        movestr = " · ".join(f"{p} {v:+.1f}%" for p, v in hits)
        alerts.append(f"{dot} **{r['name']}** ({t}): {movestr}")

    if seed:
        save_state(state | set(new_keys))
        print(f"Seeded {len(new_keys)} current moves across {len(alerts)} names — "
              "future runs will alert only NEW moves.")
        return

    if not alerts:
        print("No new outsized moves.")
        return

    title = f"🚨 Outsized moves — {len(alerts)} name(s) — {pd.Timestamp.now():%Y-%m-%d %H:%M}"
    status, body = send_teams(url, title, alerts)
    if status in (200, 202):
        save_state(state | set(new_keys))
        print(f"Alerted {len(alerts)} names (HTTP {status}).")
    else:
        print(f"Send FAILED (HTTP {status}): {body}. State not updated, will retry next run.")


if __name__ == "__main__":
    main()
