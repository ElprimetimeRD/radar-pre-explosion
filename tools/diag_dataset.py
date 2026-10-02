"""Historial para estudiar avisos rápidos (GitHub Actions, rama diag-velas; no toca el servicio).

1. Universo amplio: small y mid caps de EE. UU. con volumen (pantallas de Yahoo) + lo que vio el semáforo.
2. Velas diarias → por cada día: las que más corrieron (de la apertura al máximo, con volumen) y controles al azar.
3. Velas de 1 min de esas acciones (Yahoo guarda ~30 días) → por acción y día: velas de la sesión, curva base de
   volumen de los 4 días previos (la del RVOL del semáforo), cierre previo y ATR.
Escribe data/diag/ds_bars.csv.gz y data/diag/ds_meta.json.gz."""
from __future__ import annotations

import gzip
import json
import random
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import yfinance as yf

ET = ZoneInfo("America/New_York")
EXCH = ["NMS", "NYQ", "NGM", "NCM", "ASE"]
N_MOVERS, N_CONTROLS = 22, 22
OUT = "data/diag"


def pool() -> list[str]:
    Q = yf.EquityQuery
    base = [Q("is-in", ["exchange", *EXCH]), Q("gte", ["intradayprice", 1]), Q("gt", ["avgdailyvol3m", 300000])]
    out = set()
    for lo, hi, pages in ((5e7, 2e9, 3), (2e9, 3e10, 2)):
        q = Q("and", base + [Q("btwn", ["intradaymarketcap", lo, hi])])
        for p in range(pages):
            for sort in ("avgdailyvol3m", "percentchange"):
                try:
                    r = yf.screen(q, size=250, offset=250 * p, sortField=sort, sortAsc=False)
                except Exception as e:  # noqa: BLE001
                    print("screen", lo, p, sort, e)
                    continue
                out |= {x["symbol"] for x in (r or {}).get("quotes", []) if x.get("symbol") and "." not in x["symbol"]}
                time.sleep(1)
    for name in ("day_gainers", "small_cap_gainers", "most_actives", "aggressive_small_caps"):
        try:
            r = yf.screen(name, count=250)
            out |= {x["symbol"] for x in (r or {}).get("quotes", []) if x.get("symbol") and "." not in x["symbol"]}
        except Exception as e:  # noqa: BLE001
            print("screen", name, e)
    try:  # lo que ya vio el semáforo
        j = json.load(open(f"{OUT}/latencia.json"))
        out |= {r["t"] for r in j.get("snapshot", [])} | {m["t"] for m in j.get("movers", [])}
    except Exception:  # noqa: BLE001
        pass
    return sorted(out)


def dl(tickers, **kw):
    """yf.download por lotes de 40, con un reintento si vuelve vacío. {ticker: DataFrame}."""
    out = {}
    for i in range(0, len(tickers), 40):
        chunk = tickers[i:i + 40]
        for attempt in range(2):
            try:
                df = yf.download(chunk, group_by="ticker", auto_adjust=True, threads=True, progress=False, **kw)
            except Exception as e:  # noqa: BLE001
                print("download", e)
                df = None
            if df is not None and not df.empty:
                break
            time.sleep(5)
        if df is None or df.empty:
            continue
        for s in chunk:
            try:
                x = (df[s] if isinstance(df.columns, pd.MultiIndex) else df).dropna(how="all")
            except KeyError:
                continue
            if not x.empty:
                out[s] = x
        time.sleep(1.5)
    return out


def main():
    rng = random.Random(7)
    P = pool()
    print("universo:", len(P))
    daily = dl(P + ["SPY", "QQQ"], period="4mo", interval="1d")
    today = datetime.now(ET).date()
    # Días con velas de 1 min disponibles (Yahoo: últimos 30 días) y 4 días previos para la curva base
    first_1m = today - timedelta(days=29)
    days = sorted({d.date() for d in daily["SPY"].index if first_1m <= d.date() < today})
    study = days[4:]
    print("días de estudio:", study[0], "→", study[-1], len(study))
    pick: dict[date, dict[str, str]] = {}
    for d in study:
        rows = []
        for s, x in daily.items():
            if s in ("SPY", "QQQ"):
                continue
            x = x.dropna(subset=["Close"])
            idx = [i.date() for i in x.index]
            if d not in idx:
                continue
            k = idx.index(d)
            if k == 0:
                continue
            o, h, c, v = (float(x["Open"].iloc[k]), float(x["High"].iloc[k]), float(x["Close"].iloc[k]),
                          float(x["Volume"].iloc[k]))
            if o <= 0 or c * v < 10e6 or c < 1:
                continue
            rows.append((s, h / o - 1))
        rows.sort(key=lambda r: -r[1])
        movers = [s for s, up in rows if up >= 0.05][:N_MOVERS]
        rest = [s for s, up in rows if s not in movers]
        controls = rng.sample(rest, min(N_CONTROLS, len(rest)))
        pick[d] = {**{s: "mover" for s in movers}, **{s: "control" for s in controls}}
    need = sorted({s for v in pick.values() for s in v} | {"SPY", "QQQ"})
    print("acciones con velas de 1 min:", len(need))
    one = {}
    start = days[0]
    while start < today:  # ventanas de 7 días (límite de Yahoo para 1 min)
        end = min(start + timedelta(days=7), today)
        part = dl(need, start=start.isoformat(), end=end.isoformat(), interval="1m", prepost=False)
        for s, x in part.items():
            one.setdefault(s, []).append(x)
        start = end
    bars_rows, meta = [], []
    for s, parts in one.items():
        x = pd.concat(parts)
        x = x[~x.index.duplicated()].sort_index()
        x.index = x.index.tz_convert(ET) if x.index.tz is not None else x.index.tz_localize("UTC").tz_convert(ET)
        x = x.dropna(subset=["Close"])
        x["m"] = x.index.hour * 60 + x.index.minute
        x["day"] = [i.date() for i in x.index]
        x = x[(x["m"] >= 570) & (x["m"] < 960)]
        x["Volume"] = x["Volume"].fillna(0)
        dset = sorted(set(x["day"]))
        dd = daily.get(s)
        for d in dset:
            role = "regimen" if s in ("SPY", "QQQ") else (pick.get(d) or {}).get(s)
            if not role:
                continue
            prev_days = [p for p in dset if p < d][-4:]
            if len(prev_days) < 2:
                continue
            curves = []
            for p in prev_days:
                g = x[x["day"] == p]
                cv = g.groupby(g["m"] - 570)["Volume"].sum().reindex(range(390), fill_value=0).cumsum()
                if cv.iloc[-1] > 0:
                    curves.append(cv.values)
            if not curves:
                continue
            base = np.mean(curves, axis=0)
            prev = atr = None
            if dd is not None:
                z = dd.dropna(subset=["Close"])
                z = z[[i.date() < d for i in z.index]]
                if not z.empty:
                    prev = float(z["Close"].iloc[-1])
                    tr = pd.concat([z["High"] - z["Low"], (z["High"] - z["Close"].shift()).abs(),
                                    (z["Low"] - z["Close"].shift()).abs()], axis=1).max(axis=1)
                    if len(z) >= 15:
                        atr = round(100 * float(tr.tail(14).mean()) / prev, 2)
            g = x[x["day"] == d]
            if len(g) < 30:
                continue
            for ts, r in g.iterrows():
                bars_rows.append((d.isoformat(), s, int(r["m"]), round(float(r["Open"]), 4), round(float(r["High"]), 4),
                                  round(float(r["Low"]), 4), round(float(r["Close"]), 4), int(r["Volume"])))
            meta.append({"day": d.isoformat(), "t": s, "role": role, "prev": prev, "atr": atr,
                         "base": [round(float(b)) for b in base]})
    pd.DataFrame(bars_rows, columns=["day", "t", "m", "o", "h", "l", "c", "v"]).to_csv(
        f"{OUT}/ds_bars.csv.gz", index=False, compression="gzip")
    with gzip.open(f"{OUT}/ds_meta.json.gz", "wt") as f:
        json.dump(meta, f)
    roles = pd.Series([m["role"] for m in meta]).value_counts().to_dict()
    print("acción-días:", len(meta), roles, "velas:", len(bars_rows))


if __name__ == "__main__":
    main()
