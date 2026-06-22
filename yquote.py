"""
yquote.py — fresh last price via Yahoo's crumb-authenticated quote endpoint.

WHY THIS EXISTS
The dashboard and the alerter read daily closes from Yahoo's *chart* endpoint
(``yf.download(..., interval="1d")``). That endpoint serves the LATEST bar as NaN
until a session fully settles, so ``.dropna()`` silently fell back to the prior
close — the whole board ran ONE SESSION STALE (e.g. Acbel showed 58.1 / 18-Jun
while the real 22-Jun close was 63.9, a missed +10%).

The fresh number lives on the *quote* endpoint (``v7/finance/quote``) — the same
one the Yahoo website uses — but that requires a crumb + cookie handshake. This
module does the handshake once and batch-fetches every ticker's last price and
prior close in a couple of requests, then patches it onto the daily series.

CLOUD NOTE
This app runs on Streamlit Cloud / GitHub Actions, where Yahoo rate-limits the
hardest. Everything here is BEST-EFFORT and never raises: any failure (HTTP 429,
no crumb, timeout, parse error) yields {} / None for that name, the caller keeps
the (clearly-dated) chart close, and the UI flags how stale each name is.
"""
import datetime
import http.cookiejar
import json
import ssl
import urllib.parse
import urllib.request

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# Yahoo's certs are fine; this only avoids odd corporate-proxy CA issues on some hosts.
_CTX = ssl.create_default_context()
try:
    _CTX.check_hostname = False
    _CTX.verify_mode = ssl.CERT_NONE
except Exception:
    pass


def _opener():
    return urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()),
        urllib.request.HTTPSHandler(context=_CTX))      # attach SSL ctx here, NOT on .open()


def _get(op, url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    return op.open(req, timeout=timeout).read().decode("utf-8", "ignore")


def fetch_quotes(tickers, chunk=50):
    """{ticker: {'price': float, 'prev_close': float|None, 'asof': 'YYYY-MM-DD'}}.

    Whatever Yahoo's crumb-authenticated quote endpoint returns. Best-effort:
    names Yahoo omits are simply absent; total failure -> {}. Never raises.
    """
    tickers = [t for t in dict.fromkeys(tickers) if t]
    out = {}
    if not tickers:
        return out
    try:
        op = _opener()
        try:
            _get(op, "https://fc.yahoo.com/")              # seed consent/session cookies
        except Exception:
            pass
        crumb = _get(op, "https://query1.finance.yahoo.com/v1/test/getcrumb").strip()
        if (not crumb) or ("<" in crumb) or (len(crumb) > 50):  # got an HTML error / throttle page
            return out
        cq = urllib.parse.quote(crumb)
        for i in range(0, len(tickers), chunk):
            part = tickers[i:i + chunk]
            url = ("https://query1.finance.yahoo.com/v7/finance/quote?symbols="
                   + urllib.parse.quote(",".join(part)) + "&crumb=" + cq)
            try:
                res = json.loads(_get(op, url)).get("quoteResponse", {}).get("result", [])
            except Exception:
                continue                                    # skip this chunk, keep the rest
            for r in res:
                sym, px = r.get("symbol"), r.get("regularMarketPrice")
                if sym is None or px is None:
                    continue
                t = r.get("regularMarketTime")
                try:
                    asof = (datetime.datetime.fromtimestamp(int(t), datetime.timezone.utc)
                            .date().isoformat()) if t else ""
                except Exception:
                    asof = ""
                prev = r.get("regularMarketPreviousClose")
                out[sym] = {
                    "price": float(px),
                    "prev_close": float(prev) if prev is not None else None,
                    "asof": asof,
                }
    except Exception:
        return out
    return out


def patch_series(s, q):
    """Append/overwrite the latest daily close with the fresh quote price so the
    series ends at the true last session. Best-effort; returns ``s`` unchanged on
    any problem (so callers can blindly wrap their history dict)."""
    if s is None or q is None:
        return s
    try:
        import pandas as pd
        if len(s) == 0:
            return s
        px, asof = q.get("price"), q.get("asof")
        if px is None or not asof:
            return s
        qd = pd.Timestamp(asof)
        idx = s.index
        tz = getattr(idx, "tz", None)
        last_d = (idx[-1].tz_localize(None) if tz is not None else idx[-1]).normalize()
        if qd.normalize() < last_d:                  # quote is not newer than what we have
            return s
        s = s.copy()
        if qd.normalize() == last_d:                 # same session: overwrite the stale/NaN-derived close
            s.iloc[-1] = float(px)
        else:                                        # genuinely newer session: append a fresh bar
            label = qd.tz_localize(tz) if tz is not None else qd
            s.loc[label] = float(px)
            s = s.sort_index()
        return s
    except Exception:
        return s


def latest_date(s):
    """Last bar date of a series as a datetime.date, or None. Used for staleness flags."""
    try:
        idx = s.index
        d = idx[-1].tz_localize(None) if getattr(idx, "tz", None) is not None else idx[-1]
        return d.date()
    except Exception:
        return None


if __name__ == "__main__":          # quick self-test: python yquote.py 6282.TW 0700.HK 6702.T
    import sys
    syms = sys.argv[1:] or ["6282.TW", "0700.HK", "6702.T", "000300.SS"]
    q = fetch_quotes(syms)
    print(f"fetched {len(q)}/{len(syms)} quotes")
    for t in syms:
        print(" ", t, q.get(t))
