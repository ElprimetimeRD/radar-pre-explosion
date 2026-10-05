"""Pruebas de la estrategia de Claude (bridge/estrategia_claude.py): qué es un líder, un retroceso sano, dónde van la
compra, el stop y el objetivo, y cuándo NO hay jugada. Lógica pura. Uso: PYTHONPATH=. python tests/test_claude_estrategia.py"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "bridge"))
import estrategia_claude as S  # noqa: E402


def barras_lider(base=100.0, hasta=622, retr_vol=60_000, pausa=3):
    """Un día de líder: 20 velas tranquilas, impulso de 101 a 105, retroceso de 10 velas a 103.2 con menos volumen y
    `pausa` velas sin mínimo nuevo. Termina en la vela `hasta` (por defecto 10:22)."""
    out, m, px = [], 570, base
    for _ in range(20):                      # 9:30–9:49
        out.append((m, px, px + 0.15, px - 0.05, px + 0.05, 50_000))
        m += 1
        px += 0.05
    for _ in range(20):                      # impulso 9:50–10:09
        out.append((m, px, px + 0.25, px - 0.02, px + 0.2, 150_000))
        m += 1
        px += 0.2
    hod_m = m - 1                            # la última vela del impulso hace el máximo
    for i in range(10):                      # retroceso
        out.append((m, px, px + 0.05, px - 0.17, px - 0.15, retr_vol))
        m += 1
        px -= 0.15
    for _ in range(pausa):                   # la caída se detiene
        out.append((m, px, px + 0.12, px - 0.01, px + 0.06, retr_vol))
        m += 1
        px += 0.03
    return [b for b in out if b[0] <= hasta], hod_m


def test_jugada_tipica():
    bars, _ = barras_lider()
    j, motivo = S.evaluar("NVDA", bars, 100.0, 10_000_000, 0.03)
    assert j and motivo == "jugada", motivo
    hod = max(b[2] for b in bars)
    pb_lo = min(b[3] for b in bars[-13:])
    assert j["tipo"] == "lmt" and pb_lo < j["limite"] < bars[-1][4]            # compra límite entre el mínimo y el cierre
    assert j["stop"] < pb_lo and j["limite"] - j["stop"] >= 0.006 * j["limite"] - 0.011   # stop bajo el mínimo y ≥ 0.6 %
    r = round(j["limite"] - j["stop"], 2)
    assert j["trail"] == r and j["objetivo"] == round(j["limite"] + 2 * r, 2) and hod >= j["limite"] + 1.5 * r
    assert j["qty"] == int(500 // j["limite"]) and j["riesgo"] == round(j["qty"] * r, 2)
    assert 0.25 <= j["retr"] <= 0.6 and j["rvol"] > 1.3 and "pullback" in j["texto"]


def test_sin_jugada():
    bars, hod_m = barras_lider()
    base = (100.0, 10_000_000, 0.03)

    def mot(b, prev=100.0, adv=10_000_000, atr=0.03):
        j, m = S.evaluar("X", b, prev, adv, atr)
        assert j is None
        return m
    assert "no es líder" in mot(bars, prev=104.0)                       # sube solo +0.5 %
    assert "volumen bajo" in mot(bars, adv=100_000_000)                 # poco volumen para la hora
    assert mot(bars, adv=None) == "sin volumen habitual" and mot(bars, prev=None) == "sin cierre previo"
    assert mot(bars[:10]) == "pocas velas"
    assert "parabólica" in mot(bars, prev=70.0)
    assert "fuera del horario" in mot([b for b in bars if b[0] <= 585])        # 9:46: muy temprano
    # el retroceso
    sin_pb, _ = barras_lider(hasta=hod_m + 2)
    assert mot(sin_pb) == "sin retroceso en curso"                       # el máximo es de hace 2 velas
    profundo = bars[:-4] + [(b[0], b[1] - 1.2, b[2] - 1.2, b[3] - 1.2, b[4] - 1.2, b[5]) for b in bars[-4:]]
    assert any(x in mot(profundo) for x in ("profundo", "fuera de rango", "VWAP"))
    seguido, _ = barras_lider(pausa=1)
    assert mot(seguido) == "sigue haciendo mínimos"                       # la última vela hizo el mínimo o casi
    mucho_vol, _ = barras_lider(retr_vol=400_000)
    assert "volumen" in mot(mucho_vol)                                    # el retroceso trae más volumen que el impulso
    # bajo el VWAP: todo el día por debajo de lo que cotiza al final
    caro = [(b[0], b[1] + 2, b[2] + 2, b[3] + 2, b[4] + 2, b[5]) if b[0] >= 600 else b for b in bars]
    assert mot(caro, prev=96.0, atr=0.03) is not None
    assert "precio fuera de rango" in mot([(b[0], b[1] * 20, b[2] * 20, b[3] * 20, b[4] * 20, b[5]) for b in bars], prev=2000.0)
    # datos malos: nada revienta
    feo = list(bars) + [(700, None, 1, 1, 1, 1), ("x",), (701, float("nan"), 1, 1, 1, 1), (570, 1, 1, 1, 1, 1)]
    j, _ = S.evaluar("X", feo, *base)
    assert j is not None                                                  # las velas inválidas o repetidas se ignoran


def test_buscar_y_mercado():
    bars, _ = barras_lider()
    otra = [(m, o * 0.5, h * 0.5, low * 0.5, c * 0.5, v * 2) for m, o, h, low, c, v in bars]
    datos = {"AAA": {"bars": bars, "prev": 100.0, "adv": 10e6, "atr": 0.03},
             "BBB": {"bars": otra, "prev": 50.0, "adv": 20e6, "atr": 0.03},
             "CCC": {"bars": bars[:30], "prev": 100.0, "adv": 10e6, "atr": 0.03}}
    js, motivos = S.buscar(datos, (True, "SPY +0.3%"))
    assert [j["t"] for j in js] == sorted((j["t"] for j in js), key=lambda t: -next(x["score"] for x in js if x["t"] == t))
    assert {j["t"] for j in js} == {"AAA", "BBB"} and motivos["CCC"]
    js2, m2 = S.buscar(datos, (False, "SPY -1%"))
    assert js2 == [] and m2["AAA"] == "el mercado no está a favor"
    spy_mal = [(570 + i, 500.0, 500.5, 499.0, 499.5, 1e6) for i in range(30)]
    assert S.regimen(spy_mal, 510.0)[0] is False                         # -2 %
    assert S.regimen(spy_mal, 500.0)[0] is True
    assert S.regimen(None, 500.0)[0] is False and S.regimen(spy_mal, None)[0] is False


def test_curva_y_vwap():
    assert S.frac_vol(570) == 0 and S.frac_vol(960) == 1.0 and S.frac_vol(1000) == 1.0
    assert 0.2 < S.frac_vol(623) < 0.21
    assert all(S.frac_vol(m) <= S.frac_vol(m + 1) for m in range(560, 970))
    assert S.vwap([(570, 1, 3, 1, 2, 100), (571, 1, 3, 1, 2, 300)]) == 2.0 and S.vwap([(570, 1, 1, 1, 1, 0)]) is None


if __name__ == "__main__":
    test_jugada_tipica()
    test_sin_jugada()
    test_buscar_y_mercado()
    test_curva_y_vwap()
    print("OK · estrategia de Claude")
