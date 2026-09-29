"""Halts de NASDAQ (RSS público): la señal gratuita más rápida de que algo se está moviendo fuerte."""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET_XML
from datetime import datetime

import requests

from scanner.util import ET, log

URL = "https://www.nasdaqtrader.com/rss.aspx?feed=tradehalts"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"


def fetch() -> str | None:
    try:
        r = requests.get(URL, headers={"User-Agent": UA}, timeout=15)
        r.raise_for_status()
        return r.text
    except requests.RequestException as e:
        log.warning("halts: %s", e)
        return None


def parse(xml: str, now: datetime) -> dict[str, dict]:
    """{ticker: {code, time, resumed(bool), resume}} solo de hoy. El último halt de cada ticker manda."""
    out: dict[str, dict] = {}
    if not xml:
        return out
    try:
        root = ET_XML.fromstring(xml)
    except ET_XML.ParseError:
        return out
    today = now.astimezone(ET).strftime("%m/%d/%Y")
    now_s = now.astimezone(ET).strftime("%H:%M:%S")
    for item in root.iter("item"):
        f = {}
        for el in item:
            tag = re.sub(r"^\{.*\}", "", el.tag)
            f[tag] = (el.text or "").strip()
        sym = f.get("IssueSymbol", "").upper()
        if not sym or f.get("HaltDate") != today:
            continue
        resume = f.get("ResumptionTradeTime") or ""
        resumed = bool(resume) and resume <= now_s
        rec = {"code": f.get("ReasonCode", ""), "time": f.get("HaltTime", ""), "resume": resume, "resumed": resumed}
        if sym not in out or rec["time"] > out[sym]["time"]:
            out[sym] = rec
    return out
