"""Escaneo automático del Radar Pre-Explosión.

Uso:  python -m scanner.scan --horizon A     (intradía / pre-market)
      python -m scanner.scan --horizon B     (swing 1–20 días, después del cierre)
      python -m scanner.scan --horizon A --fixtures tests/fixtures   (sin red, para pruebas)
"""
from __future__ import annotations

import argparse
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from . import engine, features as F
from .outcomes import update_outcomes, build_scanlog
from .notify import telegram
from .util import DATA, ROOT, ET, fnum, log, now_et, read_json, rnd, session_phase, write_json

TOP_ROWS = 60          # filas que viajan a la página
STAGE2 = 70            # candidatos que reciben datos caros (opciones, noticias, filings)
MAX_UNIVERSE = 300
FLAG_LABEL = {**engine.VETO_LABEL, "spread": "Spread bid-ask > 2 %", "sub1a": "Precio por debajo de US$ 1",
              "hype": "Atención disparada sin catalizador verificable"}


def load_watchlist() -> list[str]:
    path = os.path.join(ROOT, "watchlist.txt")
    out = []
    try:
        for line in open(path, encoding="utf-8"):
            t = line.split("#")[0].strip().upper()
            if t:
                out.append(t)
    except OSError:
        pass
    return out


def build_universe(src, watch, ape, clusters) -> tuple[list[str], dict]:
    score, where = {}, {}
    def add(t, w, name):
        t = (t or "").upper().strip()
        if not t or not t.replace(".", "").replace("-", "").isalnum() or len(t) > 6:
            return
        score[t] = score.get(t, 0) + w
        where.setdefault(t, []).append(name)
    weights = {"subidas": 3, "short_alto": 2, "volumen": 1, "small_cap_gainers": 2, "most_actives": 1,
               "aggressive_small_caps": 1, "growth_technology_stocks": 1, "undervalued_growth_stocks": 1}
    for name, quotes in src.items():
        for q in quotes:
            if q.get("quoteType") in (None, "EQUITY"):
                add(q.get("symbol"), weights.get(name, 1), name)
    for t, a in sorted(ape.items(), key=lambda kv: -(kv[1]["mentions"] or 0))[:60]:
        add(t, 2 if (a.get("ratio") or 0) >= 2 else 1, "reddit")
    for t in clusters:
        add(t, 3, "insiders")
    for t in watch:
        add(t, 100, "watchlist")
    uni = sorted(score, key=lambda t: -score[t])[:MAX_UNIVERSE]
    return uni, where


def regime_from(hist) -> tuple[str, dict]:
    spy, vix = hist.get("SPY"), hist.get("^VIX")
    info = {}
    try:
        c = spy["Close"].dropna()
        ma50, ma200 = c.tail(50).mean(), c.tail(200).mean()
        v = float(vix["Close"].dropna().iloc[-1]) if vix is not None else None
        info = {"spy": rnd(c.iloc[-1], 2), "spyVs200": rnd(100 * (c.iloc[-1] / ma200 - 1), 1), "vix": rnd(v, 1)}
        if (v and v > 25) or c.iloc[-1] < ma200:
            return "off", info
        if v and v < 18 and c.iloc[-1] > ma50 > ma200:
            return "on", info
    except Exception:  # noqa: BLE001
        pass
    return "neutral", info


def main(horizon: str, fixtures: str | None = None, notify: bool = True):
    if fixtures:
        from tests.fixture_sources import install
        install(fixtures)
    from .sources import yahoo as Y, other as O

    H = horizon
    t0 = time.time()
    now = now_et()
    now_utc = now.astimezone(timezone.utc)
    phase = session_phase(now)
    today = now.date()
    log.info("Escaneo %s · %s · fase %s", H, now.isoformat(timespec="minutes"), phase)

    # ---------- 1. Universo ----------
    watch = load_watchlist()
    src = Y.screens(H)
    ape = O.apewisdom()
    clusters = O.openinsider_clusters(30)
    uni, where = build_universe(src, watch, ape, clusters)
    log.info("universo: %d candidatos", len(uni))

    # ---------- 2. Datos baratos para todos ----------
    quotes = Y.batch_quotes(uni)
    hist = Y.history(uni + ["SPY", "^VIX"], period="1y", interval="1d")
    regime, mkt = regime_from(hist)
    borrow = O.ibkr_borrow()
    prev_borrow = {}
    for back in range(5, 10):
        prev_borrow = read_json(os.path.join(DATA, "borrow", (today - timedelta(days=back)).isoformat() + ".json"), {})
        if prev_borrow:
            break

    base_st: dict[str, dict] = {}
    for t in uni:
        q = quotes.get(t, {})
        if q and q.get("quoteType") not in (None, "EQUITY"):
            continue
        dfe = F.daily_features(hist.get(t), hist.get("SPY"))
        px = fnum(q.get("regularMarketPrice")) or dfe.get("last")
        if not px:
            continue
        avgv = fnum(q.get("averageDailyVolume10Day")) or 0
        if t not in watch and (px < 0.5 or avgv < 100_000):
            continue
        st = {"ticker": t, "horizon": H, "seg": "auto", "regime": regime, "price": rnd(px, 4)}
        mc = fnum(q.get("marketCap"))
        if mc:
            st["mcap"] = rnd(mc / 1e6, 1)
        for k in ("pct52", "rel6m", "maxRet", "bbw", "vcp", "dryUp"):
            if H == "B" and dfe.get(k) is not None:
                st[k] = dfe[k]
        sp = F.spread_pct(q)
        if sp is not None and phase == "sesión":
            st["spread"] = sp
        b = borrow.get(t)
        if b and b.get("fee") is not None:
            st["ctb"] = rnd(b["fee"], 2)
            pb = prev_borrow.get(t)
            if pb is not None and b["fee"] >= 1.2 * max(pb, 0.25) and b["fee"] >= 5:
                st["ctbUp"] = True
        a = ape.get(t)
        if H == "A" and a and a.get("ratio"):
            st["ment"] = rnd(a["ratio"], 2)
        if H == "B" and t in clusters:
            st["insN"] = float(clusters[t]["n"])
        if H == "A":
            chg = fnum(q.get("preMarketChangePercent")) if phase == "pre-market" else fnum(q.get("regularMarketChangePercent"))
            if chg is not None:
                st["gap"] = rnd(chg, 1)
        base_st[t] = {"st": st, "q": q, "d": dfe}
    log.info("tras filtros de liquidez: %d", len(base_st))

    # snapshot de borrow fees de los candidatos (para "CTB subiendo")
    write_json(os.path.join(DATA, "borrow", today.isoformat() + ".json"),
               {t: borrow[t]["fee"] for t in base_st if t in borrow and borrow[t].get("fee") is not None})

    # ---------- 3. Etapa 1: score preliminar ----------
    prelim = sorted(base_st, key=lambda t: -(engine.evaluate(base_st[t]["st"])["score"] + 3 * len(where.get(t, []))))
    stage2 = list(dict.fromkeys([t for t in watch if t in base_st] + prelim[:STAGE2]))
    log.info("etapa 2 (datos completos): %d", len(stage2))

    # ---------- 4. Datos caros para la etapa 2 ----------
    intr = Y.history(stage2, period="5d", interval="1m", prepost=True) if H == "A" else {}
    tickmap = O.sec_ticker_map()
    start_e = (today - timedelta(days=21)).isoformat()
    end_e = (today + timedelta(days=15)).isoformat()
    ecal = Y.earnings_calendar(start_e, end_e)
    earn = {}
    if ecal is not None and not ecal.empty:
        for sym, r in ecal.iterrows():
            dt = r.get("Event Start Date")
            if dt is None or (hasattr(dt, "year") is False):
                continue
            dt = dt.to_pydatetime() if hasattr(dt, "to_pydatetime") else dt
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            rec = {"date": dt, "surprise": fnum(r.get("Surprise(%)")), "reported": fnum(r.get("Reported EPS")) is not None}
            prev = earn.get(str(sym).upper())
            if prev is None or abs((now_utc - dt).days) < abs((now_utc - prev["date"]).days):
                earn[str(sym).upper()] = rec
    splits = Y.splits_calendar((today - timedelta(days=90)).isoformat(), today.isoformat())
    rsplit = set()
    if splits is not None and not splits.empty:
        for sym, r in splits.iterrows():
            old, new = fnum(r.get("Old Share Worth")), fnum(r.get("Share Worth"))
            if old and new and new > old:   # p. ej. 1 → 10: reverse split
                rsplit.add(str(sym).upper())

    def enrich(t):
        rec = base_st[t]
        st, q = rec["st"], rec["q"]
        extra = {"news": [], "notes": []}
        inf = Y.info(t)
        fl = fnum(inf.get("floatShares"))
        if fl:
            st["float"] = rnd(fl / 1e6, 2)
        si = fnum(inf.get("shortPercentOfFloat"))
        if si is not None:
            st["siFloat"] = rnd(100 * si, 1)
        dtc = fnum(inf.get("shortRatio"))
        if dtc is not None:
            st["dtc"] = rnd(dtc, 2)
        if not st.get("mcap") and fnum(inf.get("marketCap")):
            st["mcap"] = rnd(fnum(inf["marketCap"]) / 1e6, 1)
        # opciones
        chains, has_opts = Y.option_chains(t)
        if not has_opts:
            st["optNA"] = True
        else:
            st.update(F.options_features(chains, st.get("price")))
            if phase == "pre-market":
                extra["notes"].append("Opciones: volumen de la sesión anterior.")
        # intradía
        if H == "A":
            ifx = F.intraday_features(intr.get(t), rec["d"].get("last") if phase != "sesión" else rec["d"].get("prevClose"), now)
            for k in ("gap", "pmVol", "rvol"):
                if ifx.get(k) is not None:
                    st[k] = ifx[k]
            if ifx.get("lastPx"):
                st["price"] = rnd(ifx["lastPx"], 4)
        # insiders (swing)
        if H == "B":
            ib = Y.insider_buys(t, 30)
            if ib is not None:
                st["insN"] = float(max(ib["n"], st.get("insN") or 0))
                if ib["ceo"]:
                    st["insCeo"] = True
        # catalizador
        items = Y.news(t, 8)
        extra["news"] = [{"title": n["title"], "url": n["url"], "src": n["src"], "ts": n["ts"]} for n in items[:3]]
        cls = F.classify_news(items, now_utc.timestamp(), H)
        erow = earn.get(t) or Y.earnings_dates(t)
        ec = F.earnings_catalyst(erow, now_utc, H)
        cat = ec if ec.get("catType") == "earnings" else None
        if not cat and cls.get("catType"):
            cat = {"catType": cls["catType"], "catAge": cls["catAge"]}
            extra["catTitle"] = cls.get("catTitle")
        if not cat and ec:
            cat = ec
        st.update({k: v for k, v in (cat or {"catType": "none"}).items() if k in ("catType", "catAge", "eps")})
        if ec.get("earningsIn") is not None:
            extra["notes"].append(f"Earnings en {ec['earningsIn']} días.")
        # filings y banderas
        cik = tickmap.get(t)
        ff = O.filing_flags(O.sec_submissions(cik) if cik else None, today)
        seg = engine.segment_of(st)
        if ff["offer30"] or cls.get("offer"):
            st["v_atm"] = True
            extra["notes"].append("Oferta reciente: " + (", ".join(ff["offer30"][:2]) or cls.get("offerTitle", "")))
        if ff["shelf36m"] and seg == "S":
            st["v_shelf"] = True
        if ff["eightK"]:
            extra["notes"].append(f"8-K presentado {ff['eightK']}.")
        if t in rsplit:
            st["v_rs"] = True
        lsd = fnum(inf.get("lastSplitDate"))
        lsf = str(inf.get("lastSplitFactor") or "")
        if lsd and ":" in lsf and (now_utc.timestamp() - lsd) < 90 * 86400:
            a, b = (fnum(x) for x in lsf.split(":")[:2])
            if a and b and a < b:
                st["v_rs"] = True
        if rec["d"].get("n", 999) < 240 and (st.get("float") or 999) < 10:
            st["v_ipo"] = True
        if st.get("price", 9) < 1:
            st["v_sub1"] = True
        rec["extra"] = extra
        return t

    def safe(t):
        try:
            return enrich(t)
        except Exception as e:  # noqa: BLE001 — un ticker roto nunca tumba el escaneo
            log.warning("enriquecer %s falló: %s", t, str(e)[:160])
            base_st[t].setdefault("extra", {"news": [], "notes": ["Datos incompletos en este escaneo."]})
            return t

    with ThreadPoolExecutor(max_workers=4) as ex:
        list(ex.map(safe, stage2))

    # ---------- 5. Score final y ranking ----------
    rows = []
    for t in stage2:
        rec = base_st[t]
        st = rec["st"]
        r = engine.evaluate(st)
        q = rec["q"]
        ex_ = rec.get("extra", {})
        rows.append({
            "t": t, "name": (q.get("shortName") or q.get("longName") or "")[:40], "seg": r["seg"],
            "score": rnd(r["score"], 1), "tier": r["tier"], "cov": rnd(r["coverage"], 3), "firing": r["firing"],
            "soft": len(r["soft"]), "hard": len(r["hard"]),
            "flags": [FLAG_LABEL.get(f, f) for f in r["hard"] + r["soft"]],
            "fam": {x["id"]: [rnd(x["pts"], 1), rnd(x["max"], 1)] for x in r["rows"] if not x["na"]},
            "px": st.get("price"), "gap": st.get("gap"), "mcap": st.get("mcap"),
            "cat": st.get("catType"), "catTitle": ex_.get("catTitle"), "news": ex_.get("news", []),
            "notes": ex_.get("notes", []), "src": where.get(t, []), "st": st,
        })
    order = {"trigger": 0, "alert": 1, "watch": 2, "none": 3, "veto": 4}
    rows.sort(key=lambda x: (order[x["tier"]], -x["score"]))
    counts = {k: sum(1 for r in rows if r["tier"] == k) for k in order}
    log.info("niveles: %s", counts)

    scan_id = f"{today.isoformat()}-{now.strftime('%H%M')}-{H}"
    record = {"id": scan_id, "H": H, "ts": now_utc.isoformat(), "date": today.isoformat(), "phase": phase,
              "rows": [{k: r[k] for k in ("t", "score", "tier", "cov", "firing", "soft", "hard", "px")} for r in rows]}
    write_json(os.path.join(DATA, "scans", scan_id + ".json"), record)

    # ---------- 6. Resultados de escaneos pasados y estadística ----------
    update_outcomes(today, phase)
    build_scanlog(H)

    doc = {"H": H, "id": scan_id, "generatedAt": now_utc.isoformat(), "phase": phase, "regime": regime, "market": mkt,
           "universe": len(uni), "evaluated": len(rows), "counts": counts, "seconds": round(time.time() - t0),
           "rows": rows[:TOP_ROWS]}
    write_json(os.path.join(DATA, "artifact", f"scan-{H}.json"), doc)
    write_json(os.path.join(DATA, "artifact", f"latest-{H}.json"), page_doc(doc, now, now_utc))
    log.info("listo en %ds: %s", doc["seconds"], scan_id)
    if notify:
        telegram(doc)
    return doc


def _ago(ts, now_ts):
    if not ts:
        return ""
    hh = (now_ts - ts) / 3600
    return f"hace {max(1, round(hh * 60))} min" if hh < 1 else f"hace {round(hh)} h" if hh < 36 else f"hace {round(hh / 24)} días"


def page_doc(doc: dict, now, now_utc) -> dict:
    """Documento en el formato de la colección `scans` que lee la pestaña Ranking de la página."""
    H = doc["H"]
    keep = [r for r in doc["rows"] if r["tier"] in ("trigger", "alert", "watch")]
    keep += [r for r in doc["rows"] if r not in keep][: max(0, 40 - len(keep))]
    items = []
    for r in keep:
        st = {k: v for k, v in r["st"].items() if k not in ("ticker", "horizon", "regime")}
        cat = None
        if r.get("cat") == "earnings":
            eps = st.get("eps")
            cat = {"h": "Earnings con sorpresa positiva" + (f" (EPS +{eps:.0f} %)" if eps else ""), "s": "Yahoo", "a": ""}
        elif r.get("catTitle"):
            n = next((x for x in r["news"] if x["title"] == r["catTitle"]), {})
            cat = {"h": r["catTitle"], "s": n.get("src", ""), "a": _ago(n.get("ts"), now_utc.timestamp())}
        elif r.get("cat") == "binary":
            cat = {"h": "Earnings programados: evento binario", "s": "Yahoo", "a": ""}
        elif r["news"]:
            n = r["news"][0]
            cat = {"h": "Sin catalizador clasificado. Último titular: " + n["title"], "s": n.get("src", ""), "a": _ago(n.get("ts"), now_utc.timestamp())}
        it = {"t": r["t"], "name": r["name"], "src": ", ".join(r["src"]), "q": now.strftime("%d-%b %H:%M ET"),
              "chg": r.get("gap"), "inputs": st}
        if cat:
            it["cat"] = cat
        if H == "B" and (st.get("insN") or 0) > 0:
            it["ins"] = f"{int(st['insN'])} insiders con compras en mercado abierto en 30 días" + (" (incluye CEO o CFO)" if st.get("insCeo") else "")
        if r["notes"]:
            it["note"] = " ".join(r["notes"])
        items.append(it)
    unmeasured = ["utilization", "perfil oportunista de insiders"] + (["atención (swing)"] if H == "B" else ["Google Trends"])
    return {
        "id": doc["id"], "horizon": H, "ts": int(now_utc.timestamp() * 1000), "createdAt": now_utc.isoformat(),
        "label": ("Intradía" if H == "A" else "Swing") + " · escáner GitHub · " + doc["phase"], "regime": doc["regime"],
        "universe": f"{doc['universe']} candidatos (Yahoo, Reddit, OpenInsider, watchlist); {doc['evaluated']} con datos completos",
        "measured": ["Opciones (vol/OI, P/C, IV)", "Short interest y days to cover", "Borrow fee (IBKR)", "Catalizador (titulares y earnings)",
                     "Dilución (EDGAR)"] + (["Gap y RVOL pre-market", "Menciones en Reddit"] if H == "A" else ["Insiders", "Máximo 52 s y momentum 6 m", "Compresión"]),
        "unmeasured": unmeasured,
        "notes": [f"Régimen automático: {doc['regime']}" + (f" (VIX {doc['market'].get('vix')})" if doc.get("market", {}).get("vix") else "") + ".",
                  "Catalizador clasificado por palabras clave del titular: confírmalo antes de actuar."],
        "items": items,
    }


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--horizon", choices=["A", "B"], required=True)
    ap.add_argument("--fixtures")
    ap.add_argument("--no-notify", action="store_true")
    a = ap.parse_args()
    main(a.horizon, a.fixtures, notify=not a.no_notify)
