/* ================= Motor del Radar Pre-Explosión ================= */
/* Puntos de evidencia sobre 100. Lo no medido vale 0; "no aplica" se excluye. */
const clamp = (x, lo = 0, hi = 1) => Math.min(hi, Math.max(lo, x));
const lin = (x, a, b) => clamp((x - a) / (b - a));
const logmap = (x, a, b) => (x <= 0 ? 0 : clamp(Math.log(x / a) / Math.log(b / a)));
const has = v => typeof v === 'number' && isFinite(v);
const NF1 = new Intl.NumberFormat('es-DO', { maximumFractionDigits: 1, minimumFractionDigits: 0 });
const fmt = (x, d = 1) => (has(x) ? new Intl.NumberFormat('es-DO', { maximumFractionDigits: d, minimumFractionDigits: 0 }).format(x) : '—');
const fmtFixed = (x, d = 1) => (has(x) ? new Intl.NumberFormat('es-DO', { maximumFractionDigits: d, minimumFractionDigits: d }).format(x) : '—');
const pct = x => (has(x) ? Math.round(x * 100) + ' %' : '—');

const FAM = {
  catalizador: { name: 'Catalizador', grade: 'A', blurb: 'La razón del movimiento. La fecha de un evento se conoce; su resultado no.' },
  opciones: { name: 'Flujo de opciones', grade: 'A', blurb: 'La evidencia más sólida de anticipación: el dinero informado suele entrar primero por opciones.' },
  squeeze: { name: 'Presión de cortos', grade: 'A/B', blurb: 'Condicional: solo cuenta completa si hay un detonante (catalizador o flujo de opciones).' },
  insiders: { name: 'Compras de insiders', grade: 'A', blurb: 'Compras en mercado abierto (código P), en grupo y no rutinarias. Señal de semanas a meses.' },
  estructura: { name: 'Precio y momentum', grade: 'A', blurb: 'Cerca del máximo de 52 semanas y más fuerte que el mercado.' },
  compresion: { name: 'Compresión y volumen', grade: 'C', blurb: 'Predice el tamaño del movimiento, no la dirección: se escala por momentum y opciones.' },
  volumen: { name: 'Gap y volumen relativo', grade: 'B/C', blurb: 'Coincidente: confirma que la acción está en juego. Tu screener de ballenas ya lo detecta.' },
  atencion: { name: 'Atención retail', grade: 'C', blurb: 'Anticipaba antes de 2021; hoy es confirmación y alerta de pump.' },
};

const WEIGHTS = {
  A: {
    S: { catalizador: 25, opciones: 20, squeeze: 25, volumen: 20, atencion: 10 },
    L: { catalizador: 30, opciones: 35, squeeze: 10, volumen: 15, atencion: 10 },
  },
  B: {
    S: { catalizador: 25, insiders: 20, estructura: 20, opciones: 15, squeeze: 12, compresion: 8 },
    L: { catalizador: 25, insiders: 12, estructura: 25, opciones: 25, squeeze: 5, compresion: 8 },
  },
};
const ORDER = {
  A: ['catalizador', 'opciones', 'squeeze', 'volumen', 'atencion'],
  B: ['catalizador', 'insiders', 'estructura', 'opciones', 'squeeze', 'compresion'],
};

const CAT_TYPES = [
  ['', '— sin revisar —', null],
  ['none', 'Ninguno identificado', 0],
  ['softpr', 'PR sin cifras (alianza vaga, "iniciativa IA")', 0.25],
  ['theme', 'Noticia de tema caliente (IA, nuclear, quantum, defensa, cripto)', 0.4],
  ['binary', 'Evento binario programado (PDUFA, AdCom, readout, earnings)', 0.45],
  ['analyst', 'Upgrade o revisiones de estimados al alza', 0.5],
  ['index', 'Inclusión en índice anunciada', 0.6],
  ['contract', 'Contrato o pedido con monto material', 0.7],
  ['mna', 'Rumor de M&A o 13D activista', 0.75],
  ['earnings', 'Earnings con sorpresa positiva (ya publicado)', 0.8],
  ['fda', 'Aprobación FDA o readout positivo (ya publicado)', 0.85],
];
const CAT_S = Object.fromEntries(CAT_TYPES.map(c => [c[0], c[2]]));
const CAT_AGES = [
  ['', '— sin revisar —', null],
  ['fresh', 'Menos de 12 horas', { A: 1, B: 1 }],
  ['d3', '1 a 3 días', { A: 0.6, B: 1 }],
  ['d20', '4 a 20 días', { A: 0.3, B: 0.75 }],
  ['old', 'Más de 20 días', { A: 0.1, B: 0.3 }],
  ['ahead', 'Aún no ocurre (fecha programada)', { A: 1, B: 0.9 }],
];
const AGE_M = Object.fromEntries(CAT_AGES.map(c => [c[0], c[2]]));
const INS_KINDS = [
  ['', '— sin revisar —', 0.7],
  ['opp', 'Oportunistas', 1],
  ['routine', 'Rutinarias', 0.15],
  ['plan', 'Plan 10b5-1', 0.1],
];
const INS_M = Object.fromEntries(INS_KINDS.map(c => [c[0], c[2]]));

function gapMap(g) {
  if (g <= 3) return 0;
  if (g <= 12) return lin(g, 3, 12);
  if (g <= 30) return 1;
  if (g <= 45) return 1 - 0.4 * lin(g, 30, 45);
  return 0.5;
}

/* Cada dato: dónde vive, cómo se convierte en 0–1 (s) y de dónde sacarlo. */
const FEATS = [
  // Catalizador
  { id: 'catType', fam: 'catalizador', h: ['A', 'B'], type: 'select', options: CAT_TYPES, label: 'Tipo de catalizador', wide: true,
    spec: 'valor fijo por tipo (0–0.85) × antigüedad', src: 'EDGAR 8-K, notas de prensa, calendarios FDA/earnings',
    why: 'Los catalizadores con fecha (earnings, PDUFA) son de grado A como calendario; su resultado no es predecible. Tras una sorpresa positiva de earnings el precio sigue derivando ~60 días (Bernard & Thomas).',
    potential: st => (st.catType ? 0 : 1) },
  { id: 'catAge', fam: 'catalizador', h: ['A', 'B'], type: 'select', options: CAT_AGES, label: 'Antigüedad de la noticia',
    spec: 'intradía: <12 h ×1 · 1–3 d ×0.6 · 4–20 d ×0.3 · >20 d ×0.1 · swing: 1 · 1 · 0.75 · 0.3', src: 'Hora del 8-K o de la nota', why: 'En intradía la noticia fresca manda; en swing el drift post-earnings dura semanas.' },
  { id: 'eps', fam: 'catalizador', h: ['A', 'B'], type: 'num', only: 'earnings', unit: '%', ph: '12', label: 'Sorpresa de EPS vs. consenso',
    map: v => lin(v, 0, 20), spec: 'lineal 0 % → 0 · 20 % → 1 (solo earnings)', src: 'Nasdaq earnings, Finviz',
    why: 'La magnitud de la sorpresa estandarizada (SUE) ordena el drift: decil alto − bajo ≈ +4.2 % a 60 días (Bernard & Thomas).',
    potential: st => (st.catType === 'earnings' && !has(st.eps) ? 0.3 : 0) },
  { id: 'guide', fam: 'catalizador', h: ['A', 'B'], type: 'tri', only: 'earnings', label: 'Guía elevada o revisiones al alza',
    spec: 'sí suma 0.2 al earnings', src: 'Nota de resultados, revisiones de analistas', why: 'Las revisiones de estimados al alza prolongan el momentum de earnings (grado B-A).',
    potential: st => (st.catType === 'earnings' && !st.guide ? 0.2 : 0) },

  // Opciones
  { id: 'callVolOI', fam: 'opciones', h: ['A', 'B'], type: 'num', unit: '×', ph: '2.5', label: 'Calls OTM ≤ 30 días: volumen ÷ OI',
    map: v => lin(v, 1, 4), sub: { A: 0.4, B: 0.25 }, spec: 'lineal 1× → 0 · 4× → 1', src: 'Cadena en Yahoo o Barchart, Unusual Whales',
    why: 'Volumen muy por encima del open interest indica posiciones nuevas. Antes del ~25 % de las OPAs aparece en calls OTM cortas, en promedio 21 días antes (Augustin, Brenner & Subrahmanyam 2019). Ignóralo si el total es < 500 contratos.' },
  { id: 'putCall', fam: 'opciones', h: ['A', 'B'], type: 'num', unit: '', ph: '0.60', label: 'Put/Call de volumen del día',
    map: v => lin(v, 1, 0.35), sub: { A: 0.3, B: 0.25 }, spec: 'lineal 1.0 → 0 · 0.35 → 1', src: 'Barchart, Unusual Whales',
    why: 'Acciones con P/C de apertura bajo superan a las de P/C alto en >40 pb al día siguiente y >1 % en la semana (Pan & Poteshman 2006). El P/C público mezcla aperturas y cierres: es un proxy ruidoso.' },
  { id: 'ivSpread', fam: 'opciones', h: ['A', 'B'], type: 'num', unit: 'pts', ph: '1.5', label: 'Spread IV call − put (ATM, mismo vencimiento)',
    map: v => lin(v, -1, 3), sub: { A: 0.2, B: 0.3 }, spec: 'lineal −1 → 0 · +3 → 1', src: 'Columna IV de la cadena (Yahoo), ORATS',
    why: 'Calls relativamente caras frente a puts ≈ +50 pb por semana (Cremers & Weinbaum 2010). El efecto se ha debilitado con el tiempo.' },
  { id: 'smirk', fam: 'opciones', h: ['A', 'B'], type: 'num', unit: 'pts', ph: '5', label: 'Smirk: IV put OTM − IV call ATM',
    map: v => lin(v, 12, 3), sub: { A: 0.1, B: 0.2 }, spec: 'lineal 12 → 0 · 3 → 1 (empinado resta)', src: 'Cadena de opciones, ORATS',
    why: 'Un smirk empinado predice ~10.9 % anual MENOS de retorno ajustado por riesgo (Xing, Zhang & Zhao 2010). Plano suma; empinado no.' },

  // Presión de cortos
  { id: 'util', fam: 'squeeze', h: ['A', 'B'], type: 'num', unit: '%', ph: '85', label: 'Utilization',
    map: v => lin(v, 70, 97), sub: { A: 0.35, B: 0.35 }, spec: 'lineal 70 % → 0 · 97 % → 1', src: 'Ortex, Fintel, S3 (de pago)',
    why: 'Porcentaje de acciones prestables ya prestadas: el mejor predictor individual de squeezes (JFQA, "Short Squeezes and Their Consequences").' },
  { id: 'ctb', fam: 'squeeze', h: ['A', 'B'], type: 'num', unit: '% anual', ph: '12', label: 'Cost to borrow',
    map: (v, st) => clamp(logmap(v, 3, 50) + (st.ctbUp ? 0.25 : 0)), sub: { A: 0.25, B: 0.25 }, spec: 'logarítmica 3 % → 0 · 50 % → 1; +0.25 si sube', src: 'iBorrowDesk (gratis), Ortex',
    why: 'Un fee que sube indica préstamo escaso. En GameStop pasó de ~1 % a 34 %.' },
  { id: 'ctbUp', fam: 'squeeze', h: ['A', 'B'], type: 'check', label: 'El CTB subió en los últimos 5 días', src: 'Historial en iBorrowDesk' },
  { id: 'siFloat', fam: 'squeeze', h: ['A', 'B'], type: 'num', unit: '% float', ph: '18', label: 'Short interest',
    map: v => lin(v, 10, 35), sub: { A: 0.25, B: 0.25 }, spec: 'lineal 10 % → 0 · 35 % → 1', src: 'Finviz "Short Float" (FINRA, cada 2 semanas)',
    why: 'Solo, un short interest alto predice retornos MENORES (Asquith, Pathak & Ritter 2005). Suma cuando utilization y CTB confirman la presión.' },
  { id: 'dtc', fam: 'squeeze', h: ['A', 'B'], type: 'num', unit: 'días', ph: '3', label: 'Days to cover',
    map: v => lin(v, 2, 8), sub: { A: 0.15, B: 0.15 }, spec: 'lineal 2 → 0 · 8 → 1', src: 'Finviz "Short Ratio"',
    why: 'Days to cover y utilization tienen el poder predictivo más robusto en 38 países (Boehmer et al. 2022).' },

  // Insiders (swing)
  { id: 'insN', fam: 'insiders', h: ['B'], type: 'num', unit: 'personas', ph: '2', step: 1, label: 'Insiders distintos comprando (código P, 30 días)',
    spec: '1 → 0.4 · 2 → 0.75 · 3+ → 1, × perfil', src: 'OpenInsider, EDGAR Form 4',
    why: 'Solo las compras "oportunistas" predicen: ~82 pb/mes de retorno anormal ponderado por valor; las rutinarias, ~0 (Cohen, Malloy & Pomorski 2012). Réplicas 2008–2024 confirman el signo con 60–70 % menos magnitud.',
    potential: st => (has(st.insN) ? 0 : 1) },
  { id: 'insKind', fam: 'insiders', h: ['B'], type: 'select', options: INS_KINDS, label: 'Perfil de las compras', spec: 'oportunistas ×1 · sin revisar ×0.7 · rutinarias ×0.15 · plan ×0.1', src: 'Rutinaria = compra el mismo mes cada año (historial de Form 4)' },
  { id: 'insCeo', fam: 'insiders', h: ['B'], type: 'check', label: 'Incluye al CEO o al CFO', spec: '+0.15', src: 'Form 4' },

  // Precio y momentum (swing)
  { id: 'pct52', fam: 'estructura', h: ['B'], type: 'num', unit: '%', ph: '92', label: 'Precio ÷ máximo de 52 semanas',
    map: v => lin(v, 75, 98), sub: { B: 0.5 }, spec: 'lineal 75 % → 0 · 98 % → 1', src: 'Finviz "52W High" (−8 % → 92)',
    why: 'La cercanía al máximo de 52 semanas predice retornos (~0.45 %/mes) y domina al momentum clásico, sin reversión posterior (George & Hwang 2004).' },
  { id: 'rel6m', fam: 'estructura', h: ['B'], type: 'num', unit: 'pp', ph: '15', label: 'Rendimiento 6 meses menos S&P 500',
    map: v => lin(v, 0, 40), sub: { B: 0.5 }, spec: 'lineal 0 → 0 · +40 pp → 1', src: 'Finviz "Perf Half Y" menos el de SPY',
    why: 'Momentum de sección cruzada (Jegadeesh & Titman 1993): robusto, con crashes ocasionales.' },
  { id: 'maxRet', fam: 'estructura', h: ['B'], type: 'num', unit: '%', ph: '9', label: 'Mayor subida diaria del último mes',
    spec: 'penaliza hasta −8 pts entre 15 % y 40 %', src: 'Gráfico diario',
    why: 'Efecto lotería: las acciones con el mayor salto diario del mes anterior rinden >1 %/mes menos después (Bali, Cakici & Whitelaw 2011). Es una contra-señal.' },

  // Compresión (swing)
  { id: 'bbw', fam: 'compresion', h: ['B'], type: 'num', unit: 'pct', ph: '20', label: 'Ancho de Bollinger: percentil de 6 meses',
    spec: 'lineal 50 → 0 · 5 → 1 (0 = el más estrecho)', src: 'TradingView (BBW), cálculo propio',
    why: 'La compresión anticipa movimientos grandes, en cualquier dirección.',
    potential: (st, H, fams) => (!has(st.bbw) && !st.vcp ? 0.6 * (0.3 + 0.7 * Math.max(fams.estructura ? fams.estructura.s : 0, fams.opciones ? fams.opciones.s : 0)) : 0) },
  { id: 'vcp', fam: 'compresion', h: ['B'], type: 'tri', label: 'Contracción en escalones (VCP) o NR7', spec: 'sí = 0.8', src: 'Gráfico diario',
    why: 'Las tasas de éxito "de 90 %" que citan los vendors dependen del régimen y tienen sesgo de supervivencia: úsalo como filtro, no como señal.' },
  { id: 'dryUp', fam: 'compresion', h: ['B'], type: 'tri', label: 'Base con volumen seco y ruptura con RVOL ≥ 1.5', spec: 'sí suma 0.4 del bloque', src: 'Gráfico diario con volumen',
    potential: (st, H, fams) => (!st.dryUp ? 0.4 * (0.3 + 0.7 * Math.max(fams.estructura ? fams.estructura.s : 0, fams.opciones ? fams.opciones.s : 0)) : 0) },

  // Volumen (intradía)
  { id: 'gap', fam: 'volumen', h: ['A'], type: 'num', unit: '%', ph: '12', label: 'Gap pre-market',
    map: v => gapMap(v), sub: { A: 0.5 }, spec: '3 % → 0 · 12–30 % → 1 · 45 %+ → 0.5 (fade)', src: 'Tu screener, Finviz, TradingView',
    why: 'Coincidente. En small caps, ~66 % de los gaps ≥ 45 % se desvanecen, más con volumen pre-market alto y precio bajo (SmallCapLab, datos de vendor).' },
  { id: 'rvol', fam: 'volumen', h: ['A'], type: 'num', unit: '×', ph: '3', label: 'RVOL ajustado por hora',
    map: v => lin(v, 1.5, 6), sub: { A: 0.5 }, spec: 'lineal 1.5× → 0 · 6× → 1', src: 'Tu screener, Finviz "Rel Volume"',
    why: 'Confirma que la acción está en juego; tiene poca o ninguna anticipación.' },
  { id: 'pmVol', fam: 'volumen', h: ['A'], type: 'num', unit: 'M acc.', ph: '1.5', label: 'Volumen pre-market',
    spec: 'solo ajusta el fade de gaps ≥ 45 %', src: 'Tu screener, TradingView' },

  // Atención (intradía)
  { id: 'ment', fam: 'atencion', h: ['A'], type: 'num', unit: '×', ph: '2', label: 'Menciones Reddit/StockTwits vs. su media',
    spec: 'lineal 1.5× → 0 · 6× → 1; cuenta el mayor de los dos', src: 'ApeWisdom, StockTwits',
    why: 'Las recomendaciones de WallStreetBets predecían retornos antes de GameStop; esa predictibilidad desapareció después (Bradley et al. 2024).',
    potential: st => (!has(st.ment) && !has(st.trends) ? 1 : 0) },
  { id: 'trends', fam: 'atencion', h: ['A'], type: 'num', unit: '×', ph: '2', label: 'Google Trends vs. media de 3 meses',
    spec: 'lineal 1.5× → 0 · 5× → 1', src: 'Google Trends',
    why: 'Un salto de búsquedas predice precios más altos por ~2 semanas y reversión dentro del año (Da, Engelberg & Gao 2011).' },
];
const FEAT = Object.fromEntries(FEATS.map(f => [f.id, f]));

const CTX_FIELDS = [
  { id: 'price', label: 'Precio', unit: 'US$', ph: '4.20' },
  { id: 'mcap', label: 'Capitalización', unit: 'US$ M', ph: '350', mult: true },
  { id: 'float', label: 'Float', unit: 'M acc.', ph: '18', mult: true },
  { id: 'spread', label: 'Spread bid-ask', unit: '%', ph: '0.5' },
];

const VETOES = [
  { id: 'atm', label: 'Oferta o ATM activa: 424B5, S-1 o "at-the-market" en los últimos 30 días', kind: seg => (seg === 'S' ? 'hard' : 'soft') },
  { id: 'pump', label: 'Promoción pagada, alertas en chats o subida sin noticia verificable', kind: () => 'hard' },
  { id: 'shelf', label: 'S-3 shelf efectivo: puede emitir acciones en cualquier momento', kind: () => 'soft' },
  { id: 'rs', label: 'Reverse split en los últimos 90 días', kind: () => 'soft' },
  { id: 'warrants', label: 'Warrants o convertibles con ejercicio cerca del precio', kind: () => 'soft' },
  { id: 'runway', label: 'Caja para menos de 6 meses de operación', kind: () => 'soft' },
  { id: 'ipo', label: 'IPO hace menos de 12 meses con float < 10 M', kind: () => 'soft' },
  { id: 'sub1', label: 'Precio < $1 o aviso de incumplimiento de listado', kind: () => 'soft' },
  { id: 'halts', label: '3 o más halts LULD hoy', kind: () => 'soft', h: ['A'] },
];

const DEFAULT_TH = { watch: 50, alert: 65, trigger: 80 };
const DEFAULT_HIT = { A: 10, B: 20 };
const TIER_RANK = { veto: -1, none: 0, watch: 1, alert: 2, trigger: 3 };
const TIER_NAME = { none: 'Sin señal', watch: 'Watch', alert: 'Alert', trigger: 'Trigger', veto: 'Veto' };

function featValue(st, f) {
  if (f.type === 'num') return has(st[f.id]) && f.map ? f.map(st[f.id], st) : null;
  if (f.type === 'tri') return st[f.id] === 'yes' ? 1 : st[f.id] === 'no' ? 0 : null;
  return null;
}
function generic(st, H, fam) {
  let s = 0, cov = 0;
  for (const f of FEATS) {
    if (f.fam !== fam || !f.h.includes(H) || !f.sub || !f.sub[H]) continue;
    const v = featValue(st, f);
    if (v == null) continue;
    s += f.sub[H] * v;
    cov += f.sub[H];
  }
  return { s: clamp(s), cov: clamp(cov), notes: [] };
}
function famCatalizador(st, H) {
  const t = st.catType;
  if (!t) return { s: 0, cov: 0, notes: [] };
  if (t === 'none') return { s: 0, cov: 1, notes: [] };
  const notes = [];
  let s = CAT_S[t];
  if (t === 'earnings') {
    const e = has(st.eps) ? lin(st.eps, 0, 20) : 0;
    s = clamp(0.5 + 0.3 * e + 0.2 * (st.guide === 'yes' ? 1 : 0));
  }
  let mult;
  if (t === 'binary') {
    mult = !st.catAge || st.catAge === 'ahead' ? 1 : AGE_M[st.catAge][H];
    notes.push('Evento binario: anticipa el tamaño del movimiento, no la dirección. Si ya ocurrió, elige su resultado.');
  } else if (st.catAge) {
    mult = AGE_M[st.catAge][H];
  } else {
    mult = 0.8;
    notes.push('Sin antigüedad marcada: se asume ×0.8.');
  }
  if (t === 'softpr') notes.push('Un PR sin cifras es el disparador típico de las promociones en small caps.');
  return { s: clamp(s * mult), cov: 1, notes };
}
function famInsiders(st) {
  if (!has(st.insN)) return { s: 0, cov: 0, notes: [] };
  const n = st.insN;
  const base = n <= 0 ? 0 : n < 2 ? 0.4 : n < 3 ? 0.75 : 1;
  const k = INS_M[st.insKind || ''];
  const ceo = st.insCeo && n >= 1 ? 0.15 : 0;
  const notes = [];
  if (st.insKind === 'routine' || st.insKind === 'plan') notes.push('Compras rutinarias o de plan: casi sin poder predictivo.');
  if (!st.insKind && n >= 1) notes.push('Perfil sin revisar (×0.7): mira si ese insider compra el mismo mes cada año.');
  return { s: clamp((base + ceo) * k), cov: 1, notes };
}
function famCompresion(st, H, fams) {
  const b = has(st.bbw) ? lin(st.bbw, 50, 5) : null;
  const v = st.vcp === 'yes' ? 0.8 : st.vcp === 'no' ? 0 : null;
  const d = st.dryUp === 'yes' ? 1 : st.dryUp === 'no' ? 0 : null;
  let raw = 0, cov = 0;
  if (b != null || v != null) { raw += 0.6 * Math.max(b ?? 0, v ?? 0); cov += 0.6; }
  if (d != null) { raw += 0.4 * d; cov += 0.4; }
  const dirSrc = Math.max(fams.estructura ? fams.estructura.s : 0, fams.opciones ? fams.opciones.s : 0);
  const dir = 0.3 + 0.7 * dirSrc;
  const notes = cov > 0 ? [`Escalada ×${fmtFixed(dir, 2)} por la dirección que dan momentum y opciones.`] : [];
  return { s: clamp(raw * dir), cov, notes };
}
function famVolumen(st, H, seg) {
  let s = 0, cov = 0;
  const notes = [];
  if (has(st.gap)) {
    let g = gapMap(st.gap);
    if (st.gap >= 45 && seg === 'S') {
      notes.push('Gap ≥ 45 % en small cap: ~66 % se desvanecen en la sesión.');
      if (has(st.pmVol) && st.pmVol >= 5) g *= 0.8;
      if (has(st.price) && st.price < 1) g *= 0.7;
    }
    s += 0.5 * g;
    cov += 0.5;
  }
  if (has(st.rvol)) { s += 0.5 * lin(st.rvol, 1.5, 6); cov += 0.5; }
  return { s: clamp(s), cov, notes };
}
function famAtencion(st) {
  const a = has(st.ment) ? lin(st.ment, 1.5, 6) : null;
  const b = has(st.trends) ? lin(st.trends, 1.5, 5) : null;
  if (a == null && b == null) return { s: 0, cov: 0, notes: [] };
  return { s: Math.max(a ?? 0, b ?? 0), cov: 1, notes: ['Desde 2021 la atención ya no anticipa: tómala como confirmación.'] };
}

function segmentOf(st) {
  if (st.seg === 'S' || st.seg === 'L') return { seg: st.seg, why: 'elegido a mano' };
  if (has(st.mcap) && st.mcap < 2000) return { seg: 'S', why: 'cap. de US$ ' + fmt(st.mcap, 0) + ' M' };
  if (has(st.float) && st.float < 20) return { seg: 'S', why: 'float de ' + fmt(st.float, 1) + ' M' };
  if (has(st.mcap)) return { seg: 'L', why: 'cap. de US$ ' + fmt(st.mcap, 0) + ' M' };
  return { seg: 'S', why: 'supuesto: falta cap. y float' };
}

function tierOf(r, th) {
  const reasons = [];
  const need = [];
  if (r.hardN > 0) return { id: 'veto', reasons: [], need: [] };
  const nF = r.firing.length;
  let t = 'none';
  if (r.score >= th.watch) t = 'watch';
  else need.push('faltan ' + fmt(th.watch - r.score, 0) + ' pts para WATCH');
  if (r.score >= th.alert) {
    if (nF >= 2 && r.coverage >= 0.5) t = 'alert';
    else reasons.push(nF < 2 ? 'El score alcanza ALERT, pero converge solo ' + nF + ' familia (se piden 2).' : 'El score alcanza ALERT, pero la cobertura (' + pct(r.coverage) + ') es menor a 50 %.');
  } else if (t === 'watch') need.push('faltan ' + fmt(th.alert - r.score, 0) + ' pts para ALERT');
  if (t === 'alert') {
    let ok, rule;
    if (r.H === 'A') {
      const k = ['opciones', 'squeeze', 'volumen'].filter(x => r.firing.includes(x)).length;
      ok = r.firing.includes('catalizador') && k >= 2;
      rule = 'catalizador + 2 de: opciones, presión de cortos, gap/volumen';
    } else {
      ok = nF >= 3 && ['insiders', 'opciones', 'catalizador'].some(x => r.firing.includes(x));
      rule = '3 familias, una de ellas insiders, opciones o catalizador';
    }
    if (r.score >= th.trigger) {
      if (ok && r.coverage >= 0.65 && r.softN <= 1) t = 'trigger';
      else reasons.push(!ok ? 'El score alcanza TRIGGER, pero falta convergencia: ' + rule + '.' : r.coverage < 0.65 ? 'El score alcanza TRIGGER, pero la cobertura (' + pct(r.coverage) + ') es menor a 65 %.' : 'El score alcanza TRIGGER, pero hay 2+ banderas de riesgo.');
    } else {
      need.push('faltan ' + fmt(th.trigger - r.score, 0) + ' pts para TRIGGER' + (ok ? '' : ' (y convergencia: ' + rule + ')'));
    }
  }
  if (r.softN >= 3 && TIER_RANK[t] > TIER_RANK.watch) { t = 'watch'; reasons.push('3 o más banderas de riesgo: como máximo WATCH.'); }
  else if (r.softN >= 2 && TIER_RANK[t] > TIER_RANK.alert) { t = 'alert'; reasons.push('2 banderas de riesgo: como máximo ALERT.'); }
  return { id: t, reasons, need };
}

function evaluate(st, th) {
  th = th || DEFAULT_TH;
  const H = st.horizon === 'B' ? 'B' : 'A';
  const { seg, why: segWhy } = segmentOf(st);
  const W = WEIGHTS[H][seg];
  const fams = {};
  for (const id of ['opciones', 'catalizador', 'insiders', 'estructura', 'volumen', 'atencion', 'squeeze', 'compresion']) {
    if (W[id] == null) continue;
    if (id === 'opciones') fams[id] = st.optNA ? { s: 0, cov: 0, na: true, notes: ['Sin opciones listadas: el bloque no cuenta en el total.'] } : generic(st, H, id);
    else if (id === 'catalizador') fams[id] = famCatalizador(st, H);
    else if (id === 'insiders') fams[id] = famInsiders(st);
    else if (id === 'volumen') fams[id] = famVolumen(st, H, seg);
    else if (id === 'atencion') fams[id] = famAtencion(st);
    else if (id === 'compresion') fams[id] = famCompresion(st, H, fams);
    else fams[id] = generic(st, H, id);
  }
  const fires = id => fams[id] && !fams[id].na && fams[id].cov > 0 && fams[id].s >= 0.5;
  const detonator = fires('catalizador') || fires('opciones');
  if (fams.squeeze && fams.squeeze.cov > 0) {
    if (!detonator) {
      fams.squeeze.s *= 0.5;
      fams.squeeze.notes.push('Sin catalizador ni flujo de opciones que lo detonen, cuenta a la mitad.');
    }
    if (fams.squeeze.s >= 0.5) fams.squeeze.notes.push('Estas acciones tienen retorno MEDIO negativo: el setup solo sube la probabilidad de la cola derecha.');
  }

  let total = 0, earned = 0, covW = 0;
  const rows = [];
  for (const id of ORDER[H]) {
    if (W[id] == null) continue;
    const r = fams[id];
    const row = { id, name: FAM[id].name, grade: FAM[id].grade, w: W[id], s: r.s, cov: r.cov, na: !!r.na, notes: r.notes || [] };
    if (!row.na) { total += row.w; earned += row.w * r.s; covW += row.w * r.cov; }
    row.firing = !row.na && r.cov > 0 && r.s >= 0.5;
    rows.push(row);
  }
  const scale = total > 0 ? 100 / total : 0;
  rows.forEach(r => { r.max = r.na ? 0 : r.w * scale; r.pts = r.na ? 0 : r.w * r.s * scale; });
  const base = earned * scale;
  const coverage = total > 0 ? covW / total : 0;

  const hard = [], soft = [];
  for (const v of VETOES) {
    if (v.h && !v.h.includes(H)) continue;
    if (st['v_' + v.id]) (v.kind(seg) === 'hard' ? hard : soft).push({ id: v.id, label: v.label });
  }
  if (has(st.spread) && st.spread > 2) soft.push({ id: 'spread', auto: true, label: 'Spread de ' + fmt(st.spread, 1) + ' %: la ejecución se come parte del movimiento' });
  if (has(st.price) && st.price < 1 && !st.v_sub1) soft.push({ id: 'sub1a', auto: true, label: 'Precio por debajo de US$ 1' });
  if (H === 'A' && seg === 'S' && fams.atencion && fams.atencion.cov > 0 && fams.atencion.s >= 0.33 && ['', undefined, null, 'none', 'softpr', 'theme'].includes(st.catType))
    soft.push({ id: 'hype', auto: true, label: 'Atención disparada sin catalizador verificable (patrón de pump)' });

  const adj = [];
  if (has(st.siFloat) && st.siFloat >= 20 && ((has(st.util) && st.util < 60) || (has(st.ctb) && st.ctb < 5)) && !detonator)
    adj.push({ id: 'si', pts: -5, label: 'Short interest alto sin presión de préstamo ni detonante', cite: 'Asquith, Pathak & Ritter 2005' });
  if (H === 'B' && has(st.maxRet) && st.maxRet > 15)
    adj.push({ id: 'max', pts: -8 * lin(st.maxRet, 15, 40), label: 'Efecto lotería: ya tuvo un día de +' + fmt(st.maxRet, 0) + ' %', cite: 'Bali, Cakici & Whitelaw 2011' });
  if (st.regime === 'off') adj.push({ id: 'regime', pts: -7, label: 'Régimen risk-off', cite: 'Supuesto del modelo' });
  if (st.regime === 'on') adj.push({ id: 'regime', pts: 3, label: 'Régimen risk-on', cite: 'Supuesto del modelo' });
  let softPts = 0;
  for (const f of soft) {
    const p = Math.max(-5, -20 - softPts);
    if (p < 0) { adj.push({ id: 'soft-' + f.id, pts: p, label: f.label, cite: 'Bandera de riesgo' }); softPts += p; }
  }
  const adjTotal = adj.reduce((a, b) => a + b.pts, 0);
  const score = clamp(base + adjTotal, 0, 100);
  const firing = rows.filter(r => r.firing).map(r => r.id);
  const tier = tierOf({ H, score, coverage, firing, softN: soft.length, hardN: hard.length }, th);

  const next = [];
  for (const f of FEATS) {
    if (!f.h.includes(H) || W[f.fam] == null) continue;
    if (f.fam === 'opciones' && st.optNA) continue;
    let p = 0;
    if (f.potential) p = f.potential(st, H, fams);
    else if (f.sub && f.sub[H] && featValue(st, f) == null) p = f.sub[H];
    const pts = p * W[f.fam] * scale * (f.fam === 'squeeze' && !detonator ? 0.5 : 1);
    if (pts >= 0.5) next.push({ id: f.id, fam: f.fam, label: f.label, src: f.src, pts });
  }
  next.sort((a, b) => b.pts - a.pts);
  const ceiling = clamp(score + next.reduce((a, b) => a + b.pts, 0), 0, 100);

  return { H, seg, segWhy, rows, base, coverage, adj, hard, soft, score, firing, tier: tier.id, reasons: tier.reasons, need: tier.need, next, ceiling, detonator };
}

if (typeof module !== 'undefined') module.exports = { evaluate, tierOf, FEATS, WEIGHTS, DEFAULT_TH, FAM };
