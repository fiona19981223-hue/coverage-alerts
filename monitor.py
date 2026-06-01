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

try:
    from streamlit_autorefresh import st_autorefresh
except ImportError:
    st_autorefresh = None

WATCHLIST = Path(__file__).with_name("watchlist.csv")
COLS = ["Name", "Ticker", "CCY", "Price", "Mkt Cap $bn",
        "1D %", "1W %", "1M %", "3M %", "1Yr %"]
PCT = ["1D %", "1W %", "1M %", "3M %", "1Yr %"]
FX_SYMS = {"HKD": "HKDUSD=X", "CNY": "CNYUSD=X", "JPY": "JPYUSD=X",
           "THB": "THBUSD=X", "IDR": "IDRUSD=X"}
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


require_login()


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
    """Daily closes (2y) per ticker. Cached 2 min (Yahoo free data is ~15-min delayed anyway)."""
    raw = yf.download(list(tickers), period="2y", interval="1d", auto_adjust=False,
                      group_by="ticker", threads=True, progress=False)
    out = {}
    for t in tickers:
        try:
            s = raw[t]["Close"].dropna() if len(tickers) > 1 else raw["Close"].dropna()
            out[t] = s if len(s) else None
        except Exception:
            out[t] = None
    return out


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
        "1D %": pct(last, float(s.iloc[-2]) if n >= 2 else None),        # vs prior close
        "1W %": pct(last, float(s.iloc[-5]) if n >= 5 else None),        # Yahoo "5D" (5-bar window)
        "1M %": ret_asof(s, last, ldn - pd.DateOffset(months=1)),       # trailing 1M from last close
        "3M %": ret_asof(s, last, ldn - pd.DateOffset(months=3)),       # trailing 3M from last close
        "1Yr %": ret_asof(s, last, TODAY - pd.Timedelta(weeks=52)),     # 52-week change (Yahoo stat)
    }


def agg_row(rows, name):
    d = {"Name": name, "Ticker": "", "CCY": "", "Price": float("nan")}
    mc = [r["Mkt Cap $bn"] for r in rows if pd.notna(r["Mkt Cap $bn"])]
    d["Mkt Cap $bn"] = sum(mc) if mc else float("nan")
    for c in PCT:
        vals = [r[c] for r in rows if pd.notna(r[c])]
        d[c] = sum(vals) / len(vals) if vals else float("nan")
    return d


def assemble(stock_rows, include_aggs=True):
    """Group & sub-group summary rows (simple avg of %, sum of mkt cap) placed
    ABOVE their members. include_aggs=False hides all summary rows. Returns (rows, levels)."""
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
            rows.extend(sg_rows); levels.extend([0] * len(sg_rows))
    return rows, levels


def assemble_group(grp):
    """Rows for ONE group: sub-group avg rows + their stocks (no group-level row)."""
    rows, levels = [], []
    for sg in dict.fromkeys(r["Sub-group"] for r in grp):
        sg_rows = [r for r in grp if r["Sub-group"] == sg]
        if sg:
            rows.append(agg_row(sg_rows, f"–  {sg}")); levels.append(1)
        rows.extend(sg_rows); levels.extend([0] * len(sg_rows))
    return rows, levels


# "Outsized move" thresholds per horizon → cell gets a green/red background.
MOVE_THRESH = {"1D %": 5, "1W %": 10, "1M %": 20, "3M %": 30, "1Yr %": 50}


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


def ccy_from_yahoo(t: str) -> str:
    """Best-effort trading currency from a Yahoo symbol suffix."""
    t = (t or "").upper().strip()
    if t.endswith(".HK"): return "HKD"
    if t.endswith(".T"): return "JPY"
    if t.endswith((".SS", ".SZ")): return "CNY"
    if t.endswith(".BK"): return "THB"
    if t.endswith(".JK"): return "IDR"
    if t.endswith((".KS", ".KQ")): return "KRW"
    if t.startswith("^") or "." in t: return ""   # index / other exchange — leave blank
    return "USD"                                   # plain symbol = US listing


def edit_watchlist_ui(wl_all):
    st.subheader("✏️ Edit watchlist")
    st.info("Add rows at the bottom, edit any cell, or select a row and press Delete to remove it. "
            "**Ticker must be Yahoo format** — e.g. `0700.HK`, `BABA`, `600588.SS`, `6702.T`, `CPALL.BK`. "
            "Currency auto-fills on Save when blank. Auto-refresh is paused while editing.")
    cols = ["group", "subgroup", "name", "ticker", "bbg", "currency"]
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
        },
    )
    if st.button("💾 Save changes", type="primary"):
        df = edited.copy()
        for c in cols:
            df[c] = df[c].fillna("").astype(str).str.strip()
        df = df[(df["ticker"] != "") & (df["name"] != "")]
        df["currency"] = [c or ccy_from_yahoo(t) for c, t in zip(df["currency"], df["ticker"])]
        text = df[cols].to_csv(index=False)
        try:
            save_watchlist_text(text)
        except Exception as e:
            st.error(f"Save failed: {e}")
        else:
            fetch_history.clear(); fetch_fx.clear(); fetch_mktcap.clear()
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

    cols = ["group", "subgroup", "name", "ticker", "bbg", "currency"]
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
        layout = st.radio("Layout", ["Collapsible groups", "Single table"], index=0,
                          help="Collapsible: click a group header to expand/collapse its names.")
        expand_all = show_groups = True
        if layout == "Collapsible groups":
            expand_all = st.toggle("Expand all groups", value=True,
                                   help="Collapse/expand every group in one click; each also toggles individually.")
        else:
            show_groups = st.toggle("Show group / sub-group rows", value=True,
                                    help="One-click hide of all group & sub-group summary rows.")
        highlight_moves = st.toggle("Highlight big moves", value=True,
                                    help="Shade a cell green/red for outsized moves: 1D≥5% · 1W≥10% · 1M≥20% · 3M≥30% · 1Yr≥50%.")
        auto = st.toggle("Auto-refresh", value=True)
        interval = st.slider("Refresh every (seconds)", 15, 300, 60, step=15)
        st.divider()
        if st.button("🔄 Refresh now"):
            fetch_history.clear(); fetch_fx.clear(); fetch_mktcap.clear()
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
with st.spinner("Fetching prices, FX and market caps… (first load ~10–20s)"):
    hist = fetch_history(tickers)
    fx = fetch_fx()
    mcaps = fetch_mktcap(tickers)

stock_rows, failures = [], []
for _, r in wl.iterrows():
    s = hist.get(r["ticker"])
    if s is None or len(s) == 0:
        failures.append(f"{r['name']} ({r['ticker']})")
        continue
    stock_rows.append(stock_row(r, s, fx, mcaps.get(r["ticker"])))

if not stock_rows:
    st.error("No data returned. Yahoo may be rate-limiting — hit Refresh in a moment.")
    st.stop()

# summary (stocks only)
sdf = pd.DataFrame(stock_rows)
up = int((sdf["1D %"] > 0).sum()); down = int((sdf["1D %"] < 0).sum())
c1, c2, c3, c4 = st.columns(4)
c1.metric("Names", len(sdf))
c2.metric("Up / Down (1D)", f"{up} / {down}")
if sdf["1D %"].notna().any():
    g = sdf.loc[sdf["1D %"].idxmax()]; l = sdf.loc[sdf["1D %"].idxmin()]
    c3.metric(f"Top 1D · {g['Name']}", f"{g['1D %']:+.1f}%")
    c4.metric(f"Worst 1D · {l['Name']}", f"{l['1D %']:+.1f}%")

if layout == "Collapsible groups":
    for g in dict.fromkeys(r["Group"] for r in stock_rows):
        grp = [r for r in stock_rows if r["Group"] == g]
        with st.expander(group_label(g, grp), expanded=expand_all):
            grows, glevels = assemble_group(grp)
            gdf = pd.DataFrame(grows, columns=COLS).reset_index(drop=True)
            st.dataframe(style_block(gdf, glevels, highlight=highlight_moves), width="stretch",
                         hide_index=True, height=min(34 * len(gdf) + 38, 640))
else:
    rows, levels = assemble(stock_rows, include_aggs=show_groups)
    df = pd.DataFrame(rows, columns=COLS).reset_index(drop=True)
    st.dataframe(style_block(df, levels, highlight=highlight_moves), width="stretch",
                 hide_index=True, height=min(34 * len(df) + 40, 1100))

st.caption(f"Last updated **{datetime.now():%H:%M:%S}**  ·  "
           + (f"🔄 auto-refresh every {interval}s · refresh #{tick}" if auto else "⏸ auto-refresh OFF")
           + f"  ·  FX→USD: " + ", ".join(f"{k} {v:.4f}" for k, v in fx.items() if k != 'USD' and v))
if failures:
    st.warning("No price history (shown as dropped): " + ", ".join(failures)
               + ". Indices (HSI Tech, CSI Tech) and brand-new listings (e.g. Minimax) often lack Yahoo history.")
