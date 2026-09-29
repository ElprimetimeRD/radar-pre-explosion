"""Compara el motor Python con engine.js sobre entradas aleatorias. Uso: python tools/parity.py"""
import json, random, subprocess, sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scanner.engine import evaluate
R = random.Random(7)
def maybe(f, p=0.7): return f() if R.random() < p else None
cases = []
for i in range(1500):
    st = {"horizon": R.choice("AB"), "seg": R.choice(["auto", "auto", "S", "L"]), "regime": R.choice(["neutral", "on", "off"])}
    num = {"price": (0.3, 60), "mcap": (20, 90000), "float": (1, 900), "spread": (0, 4), "callVolOI": (0, 6), "putCall": (0.1, 2),
           "ivSpread": (-6, 6), "smirk": (0, 20), "util": (20, 100), "ctb": (0.2, 150), "siFloat": (0, 50), "dtc": (0, 12),
           "eps": (-20, 60), "insN": (0, 5), "pct52": (40, 100), "rel6m": (-40, 80), "maxRet": (0, 80), "bbw": (0, 100),
           "gap": (-10, 120), "rvol": (0, 20), "pmVol": (0, 20), "ment": (0, 12), "trends": (0, 8)}
    for k, (a, b) in num.items():
        v = maybe(lambda: round(R.uniform(a, b), 2), 0.6)
        if v is not None: st[k] = float(int(v)) if k == "insN" else v
    for k, opts in {"catType": ["", "none", "softpr", "theme", "binary", "analyst", "index", "contract", "mna", "earnings", "fda"],
                    "catAge": ["", "fresh", "d3", "d20", "old", "ahead"], "insKind": ["", "opp", "routine", "plan"],
                    "guide": ["", "yes", "no"], "vcp": ["", "yes", "no"], "dryUp": ["", "yes", "no"]}.items():
        st[k] = R.choice(opts)
    for k in ["ctbUp", "insCeo", "optNA", "v_atm", "v_pump", "v_shelf", "v_rs", "v_warrants", "v_runway", "v_ipo", "v_sub1", "v_halts"]:
        st[k] = R.random() < (0.12 if k.startswith("v_") or k == "optNA" else 0.4)
    cases.append(st)
json.dump(cases, open("/tmp/parity_in.json", "w"))
js = r"""
const { evaluate } = require(process.argv[1]);
const cases = require('/tmp/parity_in.json');
console.log(JSON.stringify(cases.map(c => { const r = evaluate(c); return [r.score, r.tier, r.coverage, r.firing]; })));
"""
out = subprocess.run(["node", "-e", js, os.path.abspath("tools/engine.js")], capture_output=True, text=True, check=True).stdout
jsr = json.loads(out)
bad = 0
for c, j in zip(cases, jsr):
    p = evaluate(c)
    if abs(p["score"] - j[0]) > 1e-6 or p["tier"] != j[1] or abs(p["coverage"] - j[2]) > 1e-6 or p["firing"] != j[3]:
        bad += 1
        if bad <= 3: print("MISMATCH", c, j, (p["score"], p["tier"], p["coverage"], p["firing"]))
tiers = {}
for j in jsr: tiers[j[1]] = tiers.get(j[1], 0) + 1
print(f"{len(cases)} casos, {bad} diferencias; niveles: {tiers}")
sys.exit(1 if bad else 0)
