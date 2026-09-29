"""Fuentes gratuitas fuera de Yahoo: SEC EDGAR, IBKR (borrow fee), ApeWisdom, OpenInsider."""
from __future__ import annotations

import ftplib
import io
import os
import time
from datetime import date, datetime, timedelta

import pandas as pd
import requests

from ..util import fnum, log, retry

SEC_UA = os.environ.get("SEC_USER_AGENT") or "radar-pre-explosion (github.com research tool)"
BROWSER_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"

# ---------------- SEC EDGAR ----------------
OFFER_FORMS = {"424B5", "424B4", "424B3", "424B1", "S-1", "S-1/A", "F-1", "F-1/A"}
SHELF_FORMS = {"S-3", "S-3/A", "S-3ASR", "F-3", "F-3/A", "F-3ASR"}


def sec_ticker_map() -> dict[str, int]:
    def go():
        r = requests.get("https://www.sec.gov/files/company_tickers.json", headers={"User-Agent": SEC_UA}, timeout=30)
        r.raise_for_status()
        return {v["ticker"].upper(): int(v["cik_str"]) for v in r.json().values()}
    return retry(go, what="SEC ticker map") or {}


def sec_submissions(cik: int) -> dict | None:
    def go():
        r = requests.get(f"https://data.sec.gov/submissions/CIK{cik:010d}.json", headers={"User-Agent": SEC_UA}, timeout=30)
        r.raise_for_status()
        return r.json()
    res = retry(go, tries=2, what=f"SEC CIK {cik}")
    time.sleep(0.15)  # < 10 req/s
    return res


def filing_flags(sub: dict | None, today: date) -> dict:
    """Banderas de dilución y eventos a partir de filings.recent."""
    out = {"offer30": [], "shelf36m": False, "eightK": None}
    if not sub:
        return out
    rec = (sub.get("filings") or {}).get("recent") or {}
    forms, dates = rec.get("form") or [], rec.get("filingDate") or []
    for f, d in zip(forms, dates):
        try:
            dd = datetime.strptime(d, "%Y-%m-%d").date()
        except (TypeError, ValueError):
            continue
        age = (today - dd).days
        if f in OFFER_FORMS and age <= 30:
            out["offer30"].append(f"{f} {d}")
        if f in SHELF_FORMS and age <= 3 * 365:
            out["shelf36m"] = True
        if f in ("8-K", "6-K") and age <= 1 and out["eightK"] is None:
            out["eightK"] = d
    return out


# ---------------- IBKR borrow fees (FTP público "shortstock") ----------------
def ibkr_borrow() -> dict[str, dict]:
    def go():
        buf = io.BytesIO()
        with ftplib.FTP("ftp2.interactivebrokers.com", timeout=40) as ftp:
            ftp.login("shortstock", "")
            ftp.retrbinary("RETR usa.txt", buf.write)
        return parse_ibkr(buf.getvalue().decode("latin-1"))
    return retry(go, tries=2, what="IBKR borrow") or {}


def parse_ibkr(text: str) -> dict[str, dict]:
    out, cols = {}, None
    for line in text.splitlines():
        if line.startswith("#SYM"):
            cols = [c.strip().upper() for c in line.lstrip("#").split("|")]
            continue
        if line.startswith("#") or not cols:
            continue
        parts = line.split("|")
        row = dict(zip(cols, parts))
        sym = (row.get("SYM") or "").strip().upper()
        if not sym:
            continue
        avail = (row.get("AVAILABLE") or "").replace(">", "").strip()
        out[sym] = {"fee": fnum(row.get("FEERATE")), "avail": fnum(avail)}
    return out


# ---------------- ApeWisdom ----------------
def apewisdom() -> dict[str, dict]:
    out = {}
    for page in (1, 2):
        def go(page=page):
            r = requests.get(f"https://apewisdom.io/api/v1.0/filter/all-stocks/page/{page}", timeout=20, headers={"User-Agent": BROWSER_UA})
            r.raise_for_status()
            return r.json()
        j = retry(go, tries=2, what="ApeWisdom")
        for it in (j or {}).get("results", []):
            t = str(it.get("ticker", "")).upper()
            m, m0 = fnum(it.get("mentions")) or 0, fnum(it.get("mentions_24h_ago")) or 0
            if t:
                out[t] = {"mentions": m, "prev": m0, "ratio": (m / max(m0, 1.0)) if m >= 10 else None, "rank": it.get("rank")}
    log.info("ApeWisdom: %d tickers", len(out))
    return out


# ---------------- OpenInsider (compras en grupo) ----------------
def openinsider_clusters(days=30) -> dict[str, dict]:
    def go():
        r = requests.get("http://openinsider.com/latest-cluster-buys", timeout=30, headers={"User-Agent": BROWSER_UA})
        r.raise_for_status()
        return r.text
    html = retry(go, tries=2, what="OpenInsider")
    return parse_openinsider(html, days) if html else {}


def parse_openinsider(html: str, days=30) -> dict[str, dict]:
    try:
        tables = pd.read_html(io.StringIO(html))
    except ValueError:
        return {}
    tbl = next((t for t in tables if "Ticker" in [str(c).strip() for c in t.columns]), None)
    if tbl is None:
        return {}
    tbl.columns = [str(c).strip().replace("\xa0", " ") for c in tbl.columns]
    cut = pd.Timestamp.today().normalize() - pd.Timedelta(days=days)
    out = {}
    for _, r in tbl.iterrows():
        t = str(r.get("Ticker", "")).strip().upper()
        td = pd.to_datetime(r.get("Trade Date"), errors="coerce")
        n = fnum(r.get("Ins"))
        if not t or n is None or pd.isna(td) or td < cut:
            continue
        if "P" not in str(r.get("Trade Type", "")):
            continue
        out[t] = {"n": max(n, out.get(t, {}).get("n", 0)), "date": td.date().isoformat()}
    log.info("OpenInsider clusters: %d", len(out))
    return out


def last_business_days(n: int, today: date) -> list[date]:
    d, out = today, []
    while len(out) < n:
        d -= timedelta(days=1)
        if d.weekday() < 5:
            out.append(d)
    return out
