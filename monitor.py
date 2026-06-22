"""
Asian-markets coverage monitor.

Local Streamlit dashboard over an analyst coverage list (watchlist.csv, built from
the coverage workbook by build_watchlist.py). Shows, per name and per group/sub-group:
currency, price, market cap (USD), and 1D/1W/1M/3M/1Yr price moves.

Data: free, via yfinance (Yahoo Finance). No API key.
Returns: price return (auto_adjust=False), calendar-anchored like Yahoo
         (1D = prior close; 1W/1M/3M/1Yr = close on/before that date ago).
Group / sub-group rows show the SIMPLE (equal-weight) average of each % column,
and the SUM of member market caps.

Run:  streamlit run monitor.py     (or double-click run_monitor.bat)
"""

import base64
import json
import os
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from io import StringIO
from pathlib import Path

import pandas as pd
import streamlit as st
import yfinance as yf

from yquote import fetch_quotes, patch_series, latest_date

try:
    from streamlit_autorefresh import st_autorefresh
except ImportError:
    st_autorefresh = None

WATCHLIST = Path(__file__).with_name("watchlist.csv")
COLS = ["Name", "Ticker", "CCY", "Price", "Mkt Cap $bn", "1M chart",
        "1D %", "1W %", "1M %", "3M %", "1Yr %"]
PCT = ["1D %", "1W %", "1M %", "3M %", "1Yr %"]
BENCH_GROUP = "Benchmarks"          # index/ETF rows: shown like stocks, excluded from Top/Worst metrics

# Fundamentals view (growth / quality), REPORTED FINANCIALS ONLY — LFY = last reported fiscal
# year, from Yahoo's statement feed (works on the cloud). Forward consensus (Fwd P/E, FY1) was
# scrapped: it came from quoteSummary, which cloud IPs can't reach, and this dashboard is
# cloud-only — no half-empty columns. Growth cols colored by sign; ROE / Margin are levels.
FUND_COLS = ["Name", "Ticker", "CCY",
             "Rev gr LFY %", "EPS gr LFY %", "ROE %", "Margin %"]
FUND_NUM = FUND_COLS[3:]                                   # numeric columns to simple-average
FUND_GROWTH = ["Rev gr LFY %", "EPS gr LFY %"]
FUND_LEVEL = ["ROE %", "Margin %"]
FX_SYMS = {"HKD": "HKDUSD=X", "CNY": "CNYUSD=X", "JPY": "JPYUSD=X",
           "THB": "THBUSD=X", "IDR": "IDRUSD=X", "TWD": "TWDUSD=X",
           "KRW": "KRWUSD=X", "SGD": "SGDUSD=X", "INR": "INRUSD=X",
           "GBP": "GBPUSD=X", "EUR": "EURUSD=X", "AUD": "AUDUSD=X"}
TODAY = pd.Timestamp(datetime.now()).normalize()   # trailing returns anchor to *today*, like Yahoo

st.set_page_config(page_title="Coverage Monitor", page_icon="📊", layout="wide")


# ---------------- access control ----------------
def require_login():
    try:
        pw = st.secrets.get("app_password")
    except Exception:
        pw = None
    if not pw:                       # no password configured (e.g. local) -> open
        return
    if st.session_state.get("_authed"):
        return
    st.markdown("### 🔒 Coverage Monitor")
    with st.form("login"):
        entered = st.text_input("Password", type="password")
        if st.form_submit_button("Enter") and entered == pw:
            st.session_state["_authed"] = True
            st.rerun()
    if not st.session_state.get("_authed"):
        st.stop()


# require_login()   # password gate removed at user request — re-enable this line to lock the app


# ---------------- watchlist store (GitHub-backed, local fallback) ----------------
def _gh():
    """(token, repo, path, branch) from st.secrets or env. token+repo => GitHub mode."""
    token = repo = None
    path, branch = "watchlist.csv", "main"
    try:
        s = st.secrets
        token, repo = s.get("github_token"), s.get("github_repo")
        path = s.get("watchlist_path", path)
        branch = s.get("github_branch", branch)
    except Exception:
        pass
    return (token or os.environ.get("GITHUB_TOKEN"),
            repo or os.environ.get("GITHUB_REPO"), path, branch)


def gh_enabled():
    t, r, _, _ = _gh()
    return bool(t and r)


def _gh_get():
    t, r, path, branch = _gh()
    url = f"https://api.github.com/repos/{r}/contents/{path}?ref={branch}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {t}",
                                               "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        d = json.loads(resp.read())
    return base64.b64decode(d["content"]).decode("utf-8"), d["sha"]


def _gh_put(text, sha, message):
    t, r, path, branch = _gh()
    url = f"https://api.github.com/repos/{r}/contents/{path}"
    body = {"message": message, "branch": branch,
            "content": base64.b64encode(text.encode("utf-8")).decode("ascii")}
    if sha:
        body["sha"] = sha
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="PUT",
                                 headers={"Authorization": f"Bearer {t}",
                                          "Accept": "application/vnd.github+json",
                                          "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return resp.status


@st.cache_data(ttl=60, show_spinner=False)
def _watchlist_text():
    if gh_enabled():
        return _gh_get()[0]
    return WATCHLIST.read_text(encoding="utf-8-sig")


def save_watchlist_text(text):
    if gh_enabled():
        _, sha = _gh_get()                        # fresh sha to avoid write conflicts
        _gh_put(text, sha, "dashboard: update watchlist")
    else:
        WATCHLIST.write_text(text, encoding="utf-8-sig")
    _watchlist_text.clear()


# ---------------- data ----------------
def load_watchlist() -> pd.DataFrame:
    try:
        text = _watchlist_text().lstrip("﻿")   # tolerate BOM
    except Exception as e:
        st.error(f"Couldn't load the watchlist: {e}")
        st.stop()
    wl = pd.read_csv(StringIO(text), dtype=str).fillna("")
    if "ticker" not in wl.columns:
        st.error("Watchlist has no 'ticker' column."); st.stop()
    wl["ticker"] = wl["ticker"].str.strip()
    return wl[wl["ticker"] != ""]


@st.cache_data(ttl=120, show_spinner=False)
def fetch_history(tickers: tuple) -> dict:
    """Daily closes (2y) per ticker, cached 2 min. Any ticker the big batch drops
    (Yahoo intermittently drops names from large batches, esp. from cloud IPs) is
    retried individually so it doesn't silently vanish from the dashboard."""
    raw = yf.download(list(tickers), period="2y", interval="1d", auto_adjust=False,
                      group_by="ticker", threads=True, progress=False)
    out = {}
    for t in tickers:
        try:
            s = raw[t]["Close"].dropna() if len(tickers) > 1 else raw["Close"].dropna()
            out[t] = s if len(s) else None
        except Exception:
            out[t] = None
    for t in [t for t in tickers if out.get(t) is None]:   # self-heal dropped tickers
        try:
            s = yf.Ticker(t).history(period="2y", interval="1d", auto_adjust=False)["Close"].dropna()
            out[t] = s if len(s) else None
        except Exception:
            out[t] = None
    return out


@st.cache_data(ttl=120, show_spinner=False)
def fetch_quotes_cached(tickers: tuple) -> dict:
    """Fresh last price + prev close via Yahoo's crumb quote endpoint (best-effort, cached 2 min).
    The chart feed (fetch_history) serves the latest bar as NaN until a session settles, so without
    this the whole board runs a session stale. Returns {} if Yahoo throttles us — caller degrades."""
    return fetch_quotes(list(tickers))


@st.cache_data(ttl=1800, show_spinner=False)
def fetch_fx() -> dict:
    raw = yf.download(list(FX_SYMS.values()), period="5d", progress=False, auto_adjust=False)["Close"]
    fx = {"USD": 1.0}
    for ccy, sym in FX_SYMS.items():
        try:
            fx[ccy] = float(raw[sym].dropna().iloc[-1])
        except Exception:
            fx[ccy] = None
    return fx


@st.cache_data(ttl=1800, show_spinner=False)
def fetch_mktcap(tickers: tuple) -> dict:
    """Market cap in the listing currency, via fast_info['marketCap'] (parallel)."""
    def one(t):
        try:
            return t, float(yf.Ticker(t).fast_info["marketCap"])
        except Exception:
            return t, None
    out = {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        for t, mc in ex.map(one, tickers):
            out[t] = mc
    return out


@st.cache_data(ttl=300, show_spinner=False)
def fetch_tw_spot() -> dict:
    """Fallback quotes for Taiwan names Yahoo drops: official TWSE (.TW) + TPEx (.TWO) daily
    files — last completed session's close + change for EVERY listed name, no key needed.
    Returns {code: (close, prev_close, 'YYYY-MM-DD')}. Best-effort: {} on any failure."""
    out = {}

    def _f(x):
        try:
            v = float(str(x).replace(",", "").strip())
            return v
        except (TypeError, ValueError):
            return None

    def _rocdate(s):                            # '1150610' (ROC) -> '2026-06-10'
        try:
            s = str(s).strip()
            return f"{int(s[:-4]) + 1911}-{s[-4:-2]}-{s[-2:]}"
        except Exception:
            return ""

    def _pull(url, code_key, close_key, chg_key):
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0",
                                                   "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as r:
            rows = json.loads(r.read())
        for rec in rows:
            close, chg = _f(rec.get(close_key)), _f(rec.get(chg_key))
            if close is None or chg is None:
                continue
            out[str(rec.get(code_key, "")).strip()] = (close, close - chg, _rocdate(rec.get("Date")))

    try:
        _pull("https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL",
              "Code", "ClosingPrice", "Change")
    except Exception:
        pass
    try:
        _pull("https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes",
              "SecuritiesCompanyCode", "Close", "Change")
    except Exception:
        pass
    return out


def _num(x):
    """Coerce to a finite float, else None (tolerates strings / NaN / inf from Yahoo)."""
    try:
        if x is None:
            return None
        v = float(x)
        return v if pd.notna(v) and abs(v) != float("inf") else None
    except (TypeError, ValueError):
        return None


def _clip(v, lo, hi):
    """Keep v only if it's a finite number within [lo, hi]; else None ('n.m.' → blank).
    Stops pre-revenue / sign-flip outliers (e.g. -2368% margin) from distorting group averages."""
    if v is None or pd.isna(v):
        return None
    return v if lo <= v <= hi else None


@st.cache_data(ttl=21600, show_spinner=False)
def fetch_fundamentals(tickers: tuple) -> dict:
    """Per-name growth / quality from REPORTED FINANCIALS only (cached 6h): ROE, net margin and
    last-FY growth via Yahoo's statement feed, which works from the cloud. (Forward consensus was
    scrapped — quoteSummary is cloud-blocked and this dashboard is cloud-only.)
    Only fetched for the Fundamentals view. Every field best-effort -> None."""
    def one(t):
        d = {"roe_pct": None, "margin_pct": None, "rev_lfy_pct": None, "eps_lfy_pct": None}
        try:
            tk = yf.Ticker(t)
        except Exception:
            return t, d

        # actuals & quality from the financial statements (timeseries feed — works from cloud)
        last_rev = last_ni = None
        try:
            iss = tk.income_stmt
            if iss is not None and getattr(iss, "shape", (0, 0))[1] >= 1:
                def irow(*names):
                    for nm in names:
                        if nm in iss.index:
                            s = iss.loc[nm].dropna()
                            if len(s):
                                return s
                    return None
                rev = irow("Total Revenue")
                if rev is not None:
                    last_rev = _num(rev.iloc[0])
                    if len(rev) >= 2 and _num(rev.iloc[1]) and _num(rev.iloc[1]) > 0:
                        d["rev_lfy_pct"] = (float(rev.iloc[0]) / float(rev.iloc[1]) - 1) * 100
                eps = irow("Diluted EPS", "Basic EPS")
                if eps is not None and len(eps) >= 2 and _num(eps.iloc[1]) and _num(eps.iloc[1]) > 0:
                    d["eps_lfy_pct"] = (float(eps.iloc[0]) / float(eps.iloc[1]) - 1) * 100
                ni = irow("Net Income Common Stockholders", "Net Income")
                last_ni = _num(ni.iloc[0]) if ni is not None else None
                if last_ni is not None and last_rev and last_rev > 0:
                    d["margin_pct"] = last_ni / last_rev * 100
        except Exception:
            pass
        try:                                              # ROE = net income / AVERAGE shareholders' equity
            bs = tk.balance_sheet
            if bs is not None and getattr(bs, "shape", (0, 0))[1] >= 1 and last_ni is not None:
                eqs = None
                for nm in ("Stockholders Equity", "Common Stock Equity",
                           "Total Equity Gross Minority Interest"):
                    if nm in bs.index:
                        s = bs.loc[nm].dropna()
                        if len(s):
                            eqs = s; break
                if eqs is not None:
                    e0 = _num(eqs.iloc[0])
                    e1 = _num(eqs.iloc[1]) if len(eqs) >= 2 else None
                    denom = (e0 + e1) / 2 if (e0 and e1 and e0 > 0 and e1 > 0) else e0
                    if denom and denom > 0:
                        d["roe_pct"] = last_ni / denom * 100
        except Exception:
            pass
        return t, d

    out = {}
    with ThreadPoolExecutor(max_workers=6) as ex:
        for t, d in ex.map(one, tickers):
            out[t] = d
    return out


# ------------- calculations -------------
def pct(now, then):
    if then in (None, 0) or now is None or pd.isna(then) or pd.isna(now):
        return None
    return (now / then - 1.0) * 100.0


def ret_asof(s, last_px, target_date):
    """Return vs the first close ON/AFTER target_date (Yahoo's convention).
    Returns None if history doesn't reach back to target_date (insufficient history),
    so a newly-listed name shows n/a rather than a misleadingly-short period."""
    idx = s.index.tz_localize(None) if s.index.tz is not None else s.index
    if len(idx) == 0 or idx[0] > target_date:
        return None
    after = s[idx >= target_date]
    return pct(last_px, float(after.iloc[0])) if len(after) else None


def stock_row(meta, s, fx, mcap_native):
    n = len(s)
    last = float(s.iloc[-1])
    idx = s.index
    ldn = idx[-1].tz_localize(None) if idx.tz is not None else idx[-1]   # last bar, tz-naive
    rate = fx.get(meta["currency"])
    mcap_usd = (mcap_native * rate / 1e9) if (mcap_native and rate) else float("nan")
    # Each period tuned to match the Yahoo figure an analyst would eyeball:
    return {
        "Group": meta["group"], "Sub-group": meta["subgroup"], "Name": meta["name"],
        "Ticker": meta["ticker"], "CCY": meta["currency"], "Price": last,
        "Mkt Cap $bn": mcap_usd,
        "1M chart": ",".join(f"{float(x):.5g}" for x in s.iloc[-22:]),   # ~1M closes (CSV string -> sparkline)
        "1D %": pct(last, float(s.iloc[-2]) if n >= 2 else None),        # vs prior close
        "1W %": pct(last, float(s.iloc[-5]) if n >= 5 else None),        # Yahoo "5D" (5-bar window)
        "1M %": ret_asof(s, last, ldn - pd.DateOffset(months=1)),       # trailing 1M from last close
        "3M %": ret_asof(s, last, ldn - pd.DateOffset(months=3)),       # trailing 3M from last close
        "1Yr %": ret_asof(s, last, TODAY - pd.Timedelta(weeks=52)),     # 52-week change (Yahoo stat)
    }


def agg_row(rows, name):
    d = {"Name": name, "Ticker": "", "CCY": "", "Price": float("nan"), "1M chart": None}
    mc = [r["Mkt Cap $bn"] for r in rows if pd.notna(r["Mkt Cap $bn"])]
    d["Mkt Cap $bn"] = sum(mc) if mc else float("nan")
    for c in PCT:
        vals = [r[c] for r in rows if pd.notna(r[c])]
        d[c] = sum(vals) / len(vals) if vals else float("nan")
    return d


def fund_row(meta, f):
    """One Fundamentals-view row — reported financials only (statement feed, cloud-robust)."""
    return {
        "Group": meta["group"], "Sub-group": meta["subgroup"], "Name": meta["name"],
        "Ticker": meta["ticker"], "CCY": meta["currency"],
        "Rev gr LFY %": _clip(f.get("rev_lfy_pct"), -300, 300),
        "EPS gr LFY %": _clip(f.get("eps_lfy_pct"), -300, 300),
        "ROE %": _clip(f.get("roe_pct"), -150, 150),
        "Margin %": _clip(f.get("margin_pct"), -100, 100),
    }


def fund_agg_row(rows, name):
    """Simple (equal-weight) average of each fundamentals column."""
    d = {"Name": name, "Ticker": "", "CCY": ""}
    for c in FUND_NUM:
        vals = [r[c] for r in rows if r.get(c) is not None and pd.notna(r[c])]
        d[c] = sum(vals) / len(vals) if vals else float("nan")
    return d


def sort_rows(rows, sort_by, desc):
    """Order stock rows within a bucket. 'Group order' = leave as-is; NaN/None always last."""
    if sort_by in (None, "Group order"):
        return list(rows)
    if sort_by == "Name":
        return sorted(rows, key=lambda r: str(r.get("Name", "")).lower(), reverse=desc)

    def key(r):
        v = r.get(sort_by)
        if v is None or pd.isna(v):
            return (1, 0.0)                          # missing -> last
        return (0, -float(v) if desc else float(v))
    return sorted(rows, key=key)


def assemble(stock_rows, include_aggs=True, sort_by="Group order", desc=True):
    """Group & sub-group summary rows (simple avg of %, sum of mkt cap) placed ABOVE
    their members; stocks within each bucket ordered by sort_by. Returns (rows, levels)."""
    rows, levels = [], []
    groups = list(dict.fromkeys(r["Group"] for r in stock_rows))
    for g in groups:
        grp = [r for r in stock_rows if r["Group"] == g]
        if include_aggs:
            rows.append(agg_row(grp, f"▸  {g}")); levels.append(2)           # group summary
        for sg in dict.fromkeys(r["Sub-group"] for r in grp):
            sg_rows = [r for r in grp if r["Sub-group"] == sg]
            if include_aggs and sg:
                rows.append(agg_row(sg_rows, f"–  {sg}")); levels.append(1)  # sub-group summary
            ordered = sort_rows(sg_rows, sort_by, desc)
            rows.extend(ordered); levels.extend([0] * len(ordered))
    return rows, levels


def assemble_group(grp, sort_by="Group order", desc=True):
    """Rows for ONE group: sub-group avg rows + their stocks (sorted within each)."""
    rows, levels = [], []
    for sg in dict.fromkeys(r["Sub-group"] for r in grp):
        sg_rows = [r for r in grp if r["Sub-group"] == sg]
        if sg:
            rows.append(agg_row(sg_rows, f"–  {sg}")); levels.append(1)
        ordered = sort_rows(sg_rows, sort_by, desc)
        rows.extend(ordered); levels.extend([0] * len(ordered))
    return rows, levels


# "Outsized move" thresholds per horizon → cell gets a green/red background.
MOVE_THRESH = {"1D %": 5, "1W %": 10, "1M %": 20, "3M %": 30, "1Yr %": 50}

# Make the AgGrid blend into the page (no grey box) — transparent bg, no borders.
AGGRID_CSS = {
    # theme variables (belt) ...
    ".ag-root-wrapper, .ag-theme-streamlit, .ag-theme-streamlit-dark": {
        "--ag-background-color": "transparent", "--ag-odd-row-background-color": "transparent",
        "--ag-header-background-color": "transparent", "--ag-control-panel-background-color": "transparent",
        "--ag-row-hover-color": "rgba(125,125,125,0.12)", "--ag-border-color": "rgba(128,128,128,0.22)",
        "--ag-row-border-color": "rgba(128,128,128,0.10)",
    },
    # ... and direct overrides (suspenders) on every grey container — NOT .ag-cell, so
    # the inline green/red highlight bands survive.
    (".ag-root-wrapper, .ag-body, .ag-body-viewport, .ag-center-cols-clipper, "
     ".ag-center-cols-viewport, .ag-center-cols-container, .ag-body-horizontal-scroll-viewport, "
     ".ag-header, .ag-header-viewport, .ag-header-container, .ag-floating-top, "
     ".ag-floating-top-viewport, .ag-pinned-left-cols-container, .ag-virtual-list-viewport, .ag-row"):
        {"background-color": "transparent !important"},
    ".ag-floating-top .ag-row": {"background-color": "rgba(128,128,128,0.18) !important"},
    ".ag-header": {"border-bottom": "1px solid rgba(128,128,128,0.35) !important"},
    ".ag-root-wrapper": {"border": "none !important"},
}


def style_block(df, levels, highlight=True):
    cols = list(df.columns)

    def rstyle(row):
        lvl = levels[row.name]
        if lvl == 2:      # group row: bold + stronger neutral shade
            base = "font-weight:700;background-color:rgba(128,128,128,0.22)"
        elif lvl == 1:    # sub-group row: semibold + lighter shade
            base = "font-weight:600;background-color:rgba(128,128,128,0.11)"
        else:
            base = ""
        out = []
        for c in cols:
            css = base
            if c in MOVE_THRESH:
                v = row[c]
                txt = "#9ca3af" if pd.isna(v) else ("#16a34a" if v > 0 else "#dc2626" if v < 0 else "#6b7280")
                css = (base + ";" if base else "") + f"color:{txt}"
                # outsized-move cell background (stock rows only)
                if highlight and lvl == 0 and pd.notna(v) and abs(v) >= MOVE_THRESH[c]:
                    css += ";background-color:" + ("rgba(22,163,74,0.32)" if v > 0 else "rgba(220,38,38,0.32)")
            out.append(css)
        return out

    fmt = {"Price": "{:,.2f}", "Mkt Cap $bn": "{:,.1f}"}
    fmt.update({c: "{:+.1f}%" for c in PCT})
    return df.style.apply(rstyle, axis=1).format(fmt, na_rep="—")


def render_table(df, levels, key, cols=COLS, highlight=True):
    """Interactive AgGrid (DOM-rendered → crisp on mobile, tap headers to sort, swipe to scroll).
    Aggregate rows (group/sub-group avgs) are PINNED on top so sorting the stocks doesn't jumble them.
    `cols` selects which columns render, so the same grid serves the Returns and Fundamentals views.
    Columns FLEX to fill the container (fixed widths become minWidths). Clicking a stock row selects
    it — the caller reads the returned ticker to open the 📈 detail panel. On narrow (phone) grids the
    secondary columns auto-hide so Name + the moves stay on screen. Returns the selected ticker or None."""
    try:
        from st_aggrid import AgGrid, GridOptionsBuilder, JsCode
    except ImportError:
        st.dataframe(df.drop(columns=["1M chart"], errors="ignore"), hide_index=True, width="stretch")
        return None

    main, pinned = [], []
    for row, lvl in zip(df.to_dict("records"), levels):
        if lvl == 0:
            main.append(row)
        else:
            r = {k: (None if (not isinstance(v, list) and pd.isna(v)) else v) for k, v in row.items()}
            r["_lvl"] = lvl                                                # JSON-safe: NaN -> null
            pinned.append(r)
    main_df = pd.DataFrame(main if main else [], columns=cols)

    pct_fmt = JsCode("function(p){return (p.value==null||isNaN(p.value))?'—':(p.value>=0?'+':'')+Number(p.value).toFixed(1)+'%';}")
    lvl_fmt = JsCode("function(p){return (p.value==null||isNaN(p.value))?'—':Number(p.value).toFixed(1)+'%';}")
    num2 = JsCode("function(p){return (p.value==null||isNaN(p.value))?'—':Number(p.value).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2});}")
    num1 = JsCode("function(p){return (p.value==null||isNaN(p.value))?'—':Number(p.value).toLocaleString('en-US',{minimumFractionDigits:1,maximumFractionDigits:1});}")
    # inline SVG sparkline from the row's CSV string of closes (community AgGrid — no enterprise dep).
    # CSV string because st_aggrid stringifies list columns; a CLASS component (init/getGui) because
    # this st_aggrid renders function-renderer returns through React, which rejects both HTML strings
    # (escaped to text) and DOM elements (React error #31). AgGrid mounts class components directly.
    spark = JsCode(
        "class SparkRenderer {"
        " init(p){this.eGui=document.createElement('span');var a=p.value;if(!a)return;"
        "  if(typeof a==='string'){a=a.split(',').map(Number).filter(function(x){return !isNaN(x);});}"
        "  if(!a.length||a.length<2)return;"
        "  var w=92,h=26,mn=Math.min.apply(null,a),mx=Math.max.apply(null,a);var sp=(mx-mn)||1;"
        "  var pts=a.map(function(v,i){return (i*(w-4)/(a.length-1)+2).toFixed(1)+','+((h-4)-((v-mn)*(h-8)/sp)+2).toFixed(1);}).join(' ');"
        "  var col=(a[a.length-1]>=a[0])?'#16a34a':'#dc2626';"
        "  this.eGui.innerHTML='<svg width=\"'+w+'\" height=\"'+h+'\" style=\"vertical-align:middle\">"
        "<polyline points=\"'+pts+'\" fill=\"none\" stroke=\"'+col+'\" stroke-width=\"1.4\"/></svg>';}"
        " getGui(){return this.eGui;}"
        " refresh(){return false;}"
        "}")
    row_style = JsCode("function(p){if(p.node.rowPinned){var l=p.data['_lvl'];"
                       "return l==2?{'fontWeight':'700','backgroundColor':'rgba(128,128,128,0.22)'}:"
                       "{'fontWeight':'600','backgroundColor':'rgba(128,128,128,0.11)'};}return null;}")
    sign_color = JsCode("function(p){var v=p.value;if(v==null||isNaN(v))return{'color':'#9ca3af'};"
                        "return{'color':v>0?'#16a34a':(v<0?'#dc2626':'#6b7280')};}")
    # phones: keep Name + moves visible, drop the secondary columns. IDEMPOTENT — only acts when
    # visibility actually flips (hiding columns resizes the grid and re-fires this event; acting
    # unconditionally creates an infinite resize loop that hangs the renderer).
    size_fit = JsCode(
        "function(p){var w=p.clientWidth||(p.api&&p.api.getSizesForCurrentTheme?null:null);"
        "if(!p.clientWidth)return; var small=p.clientWidth<540;"
        "var col=null;try{col=p.api.getColumn?p.api.getColumn('Ticker'):null;}catch(e){}"
        "if(!col){try{col=p.columnApi.getColumn('Ticker');}catch(e){}}"
        "if(!col)return; if(col.isVisible()===!small)return;"
        "var hide=['Ticker','CCY','Mkt Cap $bn','1M chart'];"
        "try{p.api.setColumnsVisible(hide,!small);}catch(e){try{p.columnApi.setColumnsVisible(hide,!small);}catch(_){}}}")
    hl = "true" if highlight else "false"

    def pct_style(thr):
        return JsCode("function(p){var v=p.value;if(v==null||isNaN(v))return{'color':'#9ca3af'};"
                      "var s={'color':v>0?'#16a34a':(v<0?'#dc2626':'#6b7280')};"
                      "if(%s&&!p.node.rowPinned&&Math.abs(v)>=%d){s['backgroundColor']=v>0?'rgba(22,163,74,0.32)':'rgba(220,38,38,0.32)';}"
                      "return s;}" % (hl, thr))

    gb = GridOptionsBuilder.from_dataframe(main_df)
    gb.configure_default_column(sortable=True, filter=False, resizable=True, suppressMenu=True)
    gb.configure_column("Name", pinned="left", minWidth=150, sortable=True)
    if "Ticker" in cols:
        gb.configure_column("Ticker", width=92)
    if "CCY" in cols:
        gb.configure_column("CCY", width=66)
    if "Price" in cols:
        gb.configure_column("Price", type=["numericColumn"], valueFormatter=num2, width=96)
    if "Mkt Cap $bn" in cols:
        gb.configure_column("Mkt Cap $bn", type=["numericColumn"], valueFormatter=num1, width=104)
    if "1M chart" in cols:
        gb.configure_column("1M chart", cellRenderer=spark, sortable=False, width=104)
    for c in PCT:
        if c in cols:
            gb.configure_column(c, type=["numericColumn"], valueFormatter=pct_fmt,
                                cellStyle=pct_style(MOVE_THRESH[c]), width=82)
    for c in FUND_GROWTH:
        if c in cols:
            gb.configure_column(c, type=["numericColumn"], valueFormatter=pct_fmt,
                                cellStyle=sign_color, width=104)
    for c in FUND_LEVEL:
        if c in cols:
            gb.configure_column(c, type=["numericColumn"], valueFormatter=lvl_fmt, width=88)
    opts = gb.build()
    opts["pinnedTopRowData"] = pinned
    opts["getRowStyle"] = row_style
    opts["suppressMovableColumns"] = True
    opts["onGridSizeChanged"] = size_fit
    opts["onFirstDataRendered"] = size_fit

    n = len(main_df) + len(pinned)
    AgGrid(main_df, gridOptions=opts, allow_unsafe_jscode=True, theme="streamlit",
           custom_css=AGGRID_CSS, fit_columns_on_grid_load=False, key=key,
           height=min(n * 31 + 50, 1100))
    # NB: no update_on/selection feedback — wiring grid events to Streamlit reruns made the 15
    # grids re-trigger the script in a loop. The 📈 detail chart is driven by the picker instead.
    return None


def group_label(group, grp):
    """Markdown label for a group's expander: name + simple-avg returns (colored)."""
    ga = agg_row(grp, group)

    def c(v):
        if v is None or pd.isna(v):
            return "—"
        s = f"{v:+.1f}%"
        return f":green[{s}]" if v > 0 else (f":red[{s}]" if v < 0 else s)

    mc = ga["Mkt Cap $bn"]
    mc_s = "" if pd.isna(mc) else f"  ·  ${mc:,.0f}bn"
    return (f"**▸ {group}**  ({len(grp)})   1D {c(ga['1D %'])} · 1W {c(ga['1W %'])} · "
            f"1M {c(ga['1M %'])} · 3M {c(ga['3M %'])} · 1Yr {c(ga['1Yr %'])}{mc_s}")


def fund_group_label(group, grp):
    """Markdown label for a group's expander in the Fundamentals view (simple-avg fundamentals)."""
    ga = fund_agg_row(grp, group)

    def c(v):
        if v is None or pd.isna(v):
            return "—"
        s = f"{v:+.1f}%"
        return f":green[{s}]" if v > 0 else (f":red[{s}]" if v < 0 else s)

    return (f"**▸ {group}**  ({len(grp)})   Rev LFY {c(ga['Rev gr LFY %'])} · "
            f"EPS LFY {c(ga['EPS gr LFY %'])} · ROE {c(ga['ROE %'])} · Margin {c(ga['Margin %'])}")


def detail_panel(ticker, wl_all, hist):
    """📈 Detail chart for the picked name, straight from the already-cached history
    (no extra fetch for Returns view; Fundamentals view fetches just this one name)."""
    s = (hist or {}).get(ticker)
    if s is None:
        s = fetch_history((ticker,)).get(ticker)
    row = wl_all[wl_all["ticker"] == ticker]
    name = row.iloc[0]["name"] if len(row) else ticker
    with st.container(border=True):
        if s is None or len(s) == 0:
            st.markdown(f"**📈 {name}** · `{ticker}` — no price history available.")
            return
        rng = st.radio("Range", ["1M", "3M", "6M", "1Y", "2Y"], index=3, horizontal=True,
                       key="detail_rng", label_visibility="collapsed")
        off = {"1M": pd.DateOffset(months=1), "3M": pd.DateOffset(months=3),
               "6M": pd.DateOffset(months=6), "1Y": pd.DateOffset(years=1),
               "2Y": pd.DateOffset(years=2)}[rng]
        idx = s.index.tz_localize(None) if s.index.tz is not None else s.index
        s2 = s[idx >= (idx[-1] - off)]
        last, first = float(s2.iloc[-1]), float(s2.iloc[0])
        chg = (last / first - 1) * 100 if first else 0.0
        arrow = "🟢" if chg >= 0 else "🔴"
        st.markdown(f"**📈 {name}** · `{ticker}` · last **{last:,.2f}** · "
                    f"{rng} {arrow} **{chg:+.1f}%**  ·  {len(s2)} bars")
        # Altair with zero=False — st.line_chart anchors Y at 0, which flattens a 24,000-point
        # index into an invisible wiggle. Scale to the data range like any price chart.
        # X-axis format pinned per range — Altair's auto-formatter labels short spans with
        # WEEKDAYS (Mon/Tue/…), which is useless on a 1M–6M price chart.
        import altair as alt
        cdf = s2.rename("Close").reset_index()
        cdf.columns = ["Date", "Close"]
        line_col = "#16a34a" if chg >= 0 else "#dc2626"
        x_fmt = "%d %b" if rng in ("1M", "3M") else "%b %y"      # 12 Jun · Jun 26
        ch = (alt.Chart(cdf).mark_line(color=line_col, strokeWidth=1.8)
              .encode(x=alt.X("Date:T", axis=alt.Axis(title=None, format=x_fmt,
                                                      tickCount=6, labelAngle=0)),
                      y=alt.Y("Close:Q", scale=alt.Scale(zero=False),
                              axis=alt.Axis(title=None, format="~s")),
                      tooltip=[alt.Tooltip("Date:T", format="%d %b %Y"),
                               alt.Tooltip("Close:Q", format=",.2f")])
              .properties(height=260))
        st.altair_chart(ch, use_container_width=True)


def ccy_from_yahoo(t: str) -> str:
    """Best-effort trading currency from a Yahoo symbol suffix."""
    t = (t or "").upper().strip()
    if t.endswith(".HK"): return "HKD"
    if t.endswith((".TW", ".TWO")): return "TWD"
    if t.endswith(".T"): return "JPY"
    if t.endswith((".SS", ".SZ")): return "CNY"
    if t.endswith(".BK"): return "THB"
    if t.endswith(".JK"): return "IDR"
    if t.endswith((".KS", ".KQ")): return "KRW"
    if t.endswith(".SI"): return "SGD"
    if t.endswith((".NS", ".BO")): return "INR"
    if t.endswith(".L"): return "GBP"
    if t.endswith(".AX"): return "AUD"
    if t.startswith("^") or "." in t: return ""   # index / other exchange — leave blank
    return "USD"                                   # plain symbol = US listing


def edit_watchlist_ui(wl_all):
    st.subheader("✏️ Edit watchlist")
    st.info("Add rows at the bottom, edit any cell, or select a row and press Delete to remove it. "
            "**Ticker must be Yahoo format** — e.g. `0700.HK`, `BABA`, `600588.SS`, `6702.T`, `CPALL.BK`. "
            "Currency auto-fills on Save when blank; new tickers are validated against Yahoo on Save "
            "(catches `.TW` vs `.TWO` slips). Auto-refresh is paused while editing.")
    cols = ["group", "subgroup", "name", "ticker", "bbg", "currency", "alert_thr"]
    base = wl_all.reindex(columns=cols).reset_index(drop=True)
    edited = st.data_editor(
        base, num_rows="dynamic", width="stretch", key="wl_editor", height=560,
        column_config={
            "group": st.column_config.TextColumn("Group"),
            "subgroup": st.column_config.TextColumn("Sub-group"),
            "name": st.column_config.TextColumn("Name", required=True),
            "ticker": st.column_config.TextColumn("Ticker (Yahoo)", required=True,
                                                  help="e.g. 0700.HK · BABA · 600588.SS · 6702.T · CPALL.BK"),
            "bbg": st.column_config.TextColumn("Bloomberg (optional)"),
            "currency": st.column_config.TextColumn("CCY", help="Auto-filled on Save if left blank"),
            "alert_thr": st.column_config.TextColumn("Alert ±%", help="Per-name 1D Teams-alert threshold "
                                                     "(blank = default 5; e.g. 8 for habitual movers, 3 for indices)"),
        },
    )
    if st.button("💾 Save changes", type="primary"):
        df = edited.copy()
        for c in cols:
            df[c] = df[c].fillna("").astype(str).str.strip()
        df = df[(df["ticker"] != "") & (df["name"] != "")]
        df["currency"] = [c or ccy_from_yahoo(t) for c, t in zip(df["currency"], df["ticker"])]
        # validate tickers Yahoo has never seen in this list (a 3363.TW-vs-.TWO slip once cost a name)
        new = sorted(set(df["ticker"]) - set(wl_all["ticker"].astype(str)))
        if new and not st.session_state.get("wl_save_anyway"):
            with st.spinner(f"Validating {len(new)} new/changed ticker(s) against Yahoo…"):
                def probe(t):
                    try:
                        return t, len(yf.Ticker(t).history(period="5d", interval="1d")) > 0
                    except Exception:
                        return t, False
                with ThreadPoolExecutor(max_workers=6) as ex:
                    bad = [t for t, ok in ex.map(probe, new) if not ok]
            if bad:
                st.error("Yahoo has NO price data for: **" + ", ".join(bad) + "** — check the suffix "
                         "(`.TW` main board vs `.TWO` TPEx · `.SS` Shanghai vs `.SZ` Shenzhen · `.T` Japan). "
                         "Nothing was saved. Fix the ticker and Save again — or tick below to force it.")
                st.checkbox("Save anyway (skip validation this once)", key="wl_save_anyway")
                return
        st.session_state.pop("wl_save_anyway", None)
        text = df[cols].to_csv(index=False)
        try:
            save_watchlist_text(text)
        except Exception as e:
            st.error(f"Save failed: {e}")
        else:
            fetch_history.clear(); fetch_fx.clear(); fetch_mktcap.clear()
            fetch_fundamentals.clear()
            where = "GitHub (cloud — alerts will follow)" if gh_enabled() else WATCHLIST.name
            st.success(f"Saved {len(df)} names to {where}. "
                       "Toggle off ✏️ Edit watchlist to view the live monitor.")


def reorder_watchlist_ui(wl_all):
    st.subheader("↕ Reorder coverage (drag & drop)")
    st.info("Drag a name up/down to reorder it, or drag it into another group/sub-group box to **move** it there. "
            "Then click **Save order**. (To create a brand-new group, use ✏️ Edit watchlist.)")
    try:
        from streamlit_sortables import sort_items
    except ImportError:
        st.error("Drag component not available (streamlit-sortables not installed).")
        return

    cols = ["group", "subgroup", "name", "ticker", "bbg", "currency", "alert_thr"]
    df = wl_all.reindex(columns=cols).fillna("")

    by_ticker, header_key, buckets, bidx, label_ticker = {}, {}, [], {}, {}
    for _, r in df.iterrows():
        r = r.to_dict()
        key = (r["group"], r["subgroup"])
        header = r["group"] + (f"  —  {r['subgroup']}" if r["subgroup"] else "")
        if key not in bidx:
            bidx[key] = len(buckets)
            buckets.append({"header": header, "items": []})
            header_key[header] = key
        label = f"{r['name']}  ·  {r['ticker']}"
        buckets[bidx[key]]["items"].append(label)
        label_ticker[label] = r["ticker"]
        by_ticker[r["ticker"]] = r

    new_layout = sort_items(buckets, multi_containers=True, direction="vertical", key="reorder")

    if st.button("💾 Save order", type="primary"):
        rows = []
        for bucket in new_layout:
            g, sg = header_key.get(bucket["header"], ("", ""))
            for label in bucket["items"]:
                t = label_ticker.get(label)
                if t:
                    row = dict(by_ticker[t]); row["group"], row["subgroup"] = g, sg
                    rows.append(row)
        if len(rows) != len(by_ticker):
            st.error("Reorder didn't map cleanly — not saved. Refresh and retry.")
            return
        text = pd.DataFrame(rows, columns=cols).to_csv(index=False)
        try:
            save_watchlist_text(text)
        except Exception as e:
            st.error(f"Save failed: {e}")
        else:
            fetch_history.clear(); fetch_fx.clear(); fetch_mktcap.clear()
            fetch_fundamentals.clear()
            where = "GitHub (cloud — alerts will follow)" if gh_enabled() else WATCHLIST.name
            st.success(f"New order saved to {where}. Toggle off ↕ Reorder to view the monitor.")


# ---------------- UI ----------------
st.title("📊 Coverage Monitor")
st.caption("Price return · 1D vs prior close · 1W = Yahoo 5-day · 1M/3M trailing from last close · 1Yr = 52-week change · tuned to match Yahoo · "
           "group/sub-group rows = simple avg of % and sum of mkt cap · free data via yfinance")

wl_all = load_watchlist()
all_groups = list(dict.fromkeys(wl_all["group"]))

with st.sidebar:
    st.header("Settings")
    edit_mode = st.toggle("✏️ Edit watchlist", value=False,
                          help="Add or remove names, then Save. Auto-refresh pauses while editing.")
    reorder_mode = st.toggle("↕ Reorder (drag)", value=False,
                             help="Drag names to reorder, or between group boxes to move them. Then Save.")
    if not (edit_mode or reorder_mode):
        picked = st.multiselect("Groups", all_groups, default=all_groups)
        view = st.radio("View", ["Returns", "Fundamentals"], index=0,
                        help="Returns = price moves 1D–1Yr.  Fundamentals = reported growth & quality (LFY, ROE, margin).")
        layout = st.radio("Layout", ["Collapsible groups", "Single table"], index=0,
                          help="Collapsible: click a group header to expand/collapse its names.")
        expand_all = show_groups = True
        if layout == "Collapsible groups":
            expand_all = st.toggle("Expand all groups", value=True,
                                   help="Collapse/expand every group in one click; each also toggles individually.")
        else:
            show_groups = st.toggle("Show group / sub-group rows", value=True,
                                    help="One-click hide of all group & sub-group summary rows.")
        if view == "Returns":
            highlight_moves = st.toggle("Highlight big moves", value=True,
                                        help="Shade a cell green/red for outsized moves: 1D≥5% · 1W≥10% · 1M≥20% · 3M≥30% · 1Yr≥50%.")
            sort_opts = ["Group order", "1D %", "1W %", "1M %", "3M %", "1Yr %", "Price", "Mkt Cap $bn", "Name"]
        else:
            highlight_moves = False
            sort_opts = ["Group order", "Rev gr LFY %", "EPS gr LFY %", "ROE %", "Margin %", "Name"]
        sort_by = st.selectbox("Sort names by", sort_opts, index=0,
                               help="Reorders names within each group / sub-group.")
        sort_desc = st.toggle("High → low", value=True, help="Off = low → high (A→Z for Name).")
        if view == "Returns":
            auto = st.toggle("Auto-refresh", value=True)
            interval = st.slider("Refresh every (seconds)", 15, 300, 60, step=15)
        else:
            auto, interval = False, 60
            st.caption("Fundamentals pull once when you open this view (then cached ~6h) — no auto-refresh, "
                       "to avoid Yahoo rate-limits. Hit 🔄 Refresh now to force a fresh pull.")
        st.divider()
        if st.button("🔄 Refresh now"):
            fetch_history.clear(); fetch_fx.clear(); fetch_mktcap.clear()
            fetch_fundamentals.clear()
        st.caption(f"{len(wl_all)} names from {WATCHLIST.name}. Market cap & FX cached ~30 min.")

if edit_mode:
    edit_watchlist_ui(wl_all)
    st.stop()
if reorder_mode:
    reorder_watchlist_ui(wl_all)
    st.stop()

wl = wl_all[wl_all["group"].isin(picked)] if picked else wl_all
if wl.empty:
    st.warning("No groups selected."); st.stop()

tick = st_autorefresh(interval=interval * 1000, key="auto") if (auto and st_autorefresh) else 0

tickers = tuple(wl["ticker"])
failures, foot, hist = [], None, None

if view == "Fundamentals":
    with st.spinner("Fetching reported financials… (first load ~30–60s; cached 6h)"):
        funds = fetch_fundamentals(tickers)
    rows_all = [fund_row(r, funds.get(r["ticker"]) or {}) for _, r in wl.iterrows()]
    cols_act, agg_fn, label_fn = FUND_COLS, fund_agg_row, fund_group_label
    fdf = pd.DataFrame(rows_all)
    roe_med = fdf["ROE %"].median(); mg_med = fdf["Margin %"].median()
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Names", len(fdf))
    c2.metric("Median ROE", "—" if pd.isna(roe_med) else f"{roe_med:.1f}%")
    c3.metric("Median net margin", "—" if pd.isna(mg_med) else f"{mg_med:.1f}%")
    c4.metric("With financials", f"{int(fdf['Rev gr LFY %'].notna().sum())} / {len(fdf)}",
              help="Names whose reported statements Yahoo carries (indices and brand-new listings have none).")
else:
    with st.spinner("Fetching prices, FX and market caps… (first load ~10–20s)"):
        hist = fetch_history(tickers)
        quotes = fetch_quotes_cached(tickers)                  # fresh last price (crumb quote endpoint)
        hist = {t: patch_series(s, quotes.get(t)) for t, s in hist.items()}
        fx = fetch_fx()
        mcaps = fetch_mktcap(tickers)
    name_by_t = {r["ticker"]: r["name"] for _, r in wl.iterrows()}
    _asof = {t: latest_date(s) for t, s in hist.items() if s is not None and len(s)}
    data_asof = max([d for d in _asof.values() if d], default=None)
    behind_names = [name_by_t.get(t, t) for t, d in _asof.items() if data_asof and d and d < data_asof]
    stock_rows, tw_patched = [], []
    for _, r in wl.iterrows():
        s = hist.get(r["ticker"])
        if s is None or len(s) == 0:
            q = quotes.get(r["ticker"])                  # no chart history but a live quote? Price + 1D
            if q and q.get("price") is not None and q.get("prev_close"):
                last = q["price"]
                stock_rows.append({
                    "Group": r["group"], "Sub-group": r["subgroup"], "Name": r["name"],
                    "Ticker": r["ticker"], "CCY": r["currency"], "Price": last,
                    "Mkt Cap $bn": float("nan"), "1M chart": None,
                    "1D %": pct(last, q["prev_close"]), "1W %": None, "1M %": None,
                    "3M %": None, "1Yr %": None})
                continue
            t = r["ticker"].upper()
            spot = fetch_tw_spot().get(t.split(".")[0]) if t.endswith((".TW", ".TWO")) else None
            if spot:                                    # official TWSE/TPEx EOD fallback: Price + 1D
                last, prev, asof = spot
                stock_rows.append({
                    "Group": r["group"], "Sub-group": r["subgroup"], "Name": r["name"],
                    "Ticker": r["ticker"], "CCY": r["currency"], "Price": last,
                    "Mkt Cap $bn": float("nan"), "1M chart": None,
                    "1D %": pct(last, prev), "1W %": None, "1M %": None, "3M %": None,
                    "1Yr %": None})
                tw_patched.append(f"{r['name']} (TWSE/TPEx close {asof})")
                continue
            failures.append(f"{r['name']} ({r['ticker']})")
            continue
        stock_rows.append(stock_row(r, s, fx, mcaps.get(r["ticker"])))
    if not stock_rows:
        st.error("No data returned. Yahoo may be rate-limiting — hit Refresh in a moment.")
        st.stop()
    rows_all = stock_rows
    cols_act, agg_fn, label_fn = COLS, agg_row, group_label
    sdf = pd.DataFrame(stock_rows)
    core = sdf[sdf["Group"] != BENCH_GROUP]            # indices don't compete for Top/Worst
    if core.empty:
        core = sdf
    up = int((core["1D %"] > 0).sum()); down = int((core["1D %"] < 0).sum())
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Names", len(sdf))
    c2.metric("Up / Down (1D)", f"{up} / {down}")
    if core["1D %"].notna().any():
        g = core.loc[core["1D %"].idxmax()]; l = core.loc[core["1D %"].idxmin()]
        c3.metric(f"Top 1D · {g['Name']}", f"{g['1D %']:+.1f}%")
        c4.metric(f"Worst 1D · {l['Name']}", f"{l['1D %']:+.1f}%")
    if data_asof:
        _m = f"📅 Prices as of **{data_asof:%d %b %Y}** (last completed session)"
        if behind_names:
            _m += (f"  ·  ⚠ **{len(behind_names)}** a session behind "
                   "(Yahoo quote throttled — showing last good close): "
                   + ", ".join(behind_names[:8]) + ("…" if len(behind_names) > 8 else ""))
        st.caption(_m)
    foot = "FX→USD: " + ", ".join(f"{k} {v:.4f}" for k, v in fx.items() if k != "USD" and v)

pick_col, _ = st.columns([0.45, 0.55])              # 📈 on-demand price chart for any covered name
choice = pick_col.selectbox("📈 Chart a name", [""] + [f"{r['name']}  ·  {r['ticker']}" for _, r in wl.iterrows()],
                            index=0, help="Type-ahead any name — 2y chart from the already-fetched history.")
if choice:
    detail_panel(choice.rsplit("·", 1)[-1].strip(), wl_all, hist)

if layout == "Collapsible groups":
    for g in dict.fromkeys(r["Group"] for r in rows_all):
        grp = [r for r in rows_all if r["Group"] == g]
        with st.expander(label_fn(g, grp), expanded=expand_all):
            for sg in dict.fromkeys(r["Sub-group"] for r in grp):
                sg_rows = sort_rows([r for r in grp if r["Sub-group"] == sg], sort_by, sort_desc)
                if sg:                                   # sub-group avg = one pinned row (no multi-pin bug)
                    rows = [agg_fn(sg_rows, f"–  {sg}")] + sg_rows
                    levels = [1] + [0] * len(sg_rows)
                else:                                    # group has no sub-groups: just its stocks
                    rows, levels = sg_rows, [0] * len(sg_rows)
                gdf = pd.DataFrame(rows, columns=cols_act).reset_index(drop=True)
                render_table(gdf, levels, key=f"grid_{view}_{g}_{sg or 'main'}",
                             cols=cols_act, highlight=highlight_moves)
else:
    flat = sort_rows(rows_all, sort_by, sort_desc)
    df = pd.DataFrame(flat, columns=cols_act).reset_index(drop=True)
    render_table(df, [0] * len(flat), key=f"grid_single_{view}",
                 cols=cols_act, highlight=highlight_moves)

with st.sidebar:                                        # export the CURRENT view, Excel-friendly
    exp = pd.DataFrame(rows_all).drop(columns=["1M chart"], errors="ignore")
    st.download_button("⬇ Export CSV", exp.to_csv(index=False).encode("utf-8-sig"),
                       file_name=f"coverage_{view.lower()}_{datetime.now():%Y-%m-%d}.csv",
                       mime="text/csv", help="Current view, one row per name (Excel-friendly UTF-8).")

if view == "Fundamentals":
    st.caption("Fundamentals · **ROE, Net margin & LFY growth** computed from reported financials (works on the "
               "cloud) · group/sub-group rows = simple average · cached ~6h")

st.caption(f"Last updated **{datetime.now():%H:%M:%S}**  ·  "
           + (f"🔄 auto-refresh every {interval}s · refresh #{tick}" if auto else "⏸ auto-refresh OFF")
           + (f"  ·  {foot}" if foot else ""))
if view == "Returns" and tw_patched:
    st.caption("🛟 Yahoo had no data for these — patched from the official TWSE/TPEx daily file "
               "(last completed session; Price + 1D only): " + "; ".join(tw_patched))
if view == "Returns" and failures:
    st.warning("No price history (shown as dropped): " + ", ".join(failures)
               + ". Brand-new listings (e.g. Minimax) often lack Yahoo history.")
