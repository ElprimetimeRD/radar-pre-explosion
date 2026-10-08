"""Telegram de entrada: los toques del botón «✅ Ejecutar en paper», los comandos (/estado, /pausa, /reanuda,
/cerrar, /auto, /boton, y /claude y /marcador del ejecutor paralelo de Claude) y las órdenes a mano en texto
(«XYZ 10.00 stop 9.50 tp 12.00 riesgo 50» + OK, posiciones, ordenes, cancelar: live/ordenes_tg.py, solo paper). Lee con long polling (getUpdates): Telegram responde en cuanto llega algo, sin webhook ni
dirección pública. Solo atiende al chat de TELEGRAM_CHAT_ID; el token nunca va al registro."""
from __future__ import annotations

import logging
import os
import time

import requests

log = logging.getLogger("radar")
VIEJO_S = 120   # comandos más viejos que esto (p. ej. escritos mientras el servicio reiniciaba) se ignoran


class TgIn:
    def __init__(self, paper, token: str | None = None, chat_id: str | None = None, http=None, clock=time.time,
                 user_id: str | None = None, claude=None, ordenes=None):
        self.paper = paper
        self.ordenes = ordenes  # live/ordenes_tg.py: órdenes a mano por texto (None = no existen)
        self.claude = claude    # live/claude_paper.py: /claude y /marcador (None = no existen)
        self.token = token or os.environ.get("TELEGRAM_BOT_TOKEN") or ""
        self.chat = str(chat_id or os.environ.get("TELEGRAM_CHAT_ID") or "")
        # Quién puede mandar: TELEGRAM_USER_ID si está; si no, en un chat privado el propio chat (es tu usuario). En un
        # grupo sin TELEGRAM_USER_ID nadie: cualquier miembro podría poner órdenes o cerrar todo.
        self.user = str(user_id or os.environ.get("TELEGRAM_USER_ID") or (self.chat if not self.chat.startswith("-") else ""))
        self.http = http or requests
        self.clock = clock
        self.offset: int | None = None
        self.state: dict = {"ok": None, "error": None}

    def configured(self) -> bool:
        return bool(self.token and self.chat)

    # ---------------- API de Telegram ----------------
    def api(self, method: str, payload: dict, timeout: float = 15) -> dict | None:
        try:
            r = self.http.post(f"https://api.telegram.org/bot{self.token}/{method}", json=payload, timeout=timeout)
            j = r.json() if r.content else {}
        except (requests.RequestException, ValueError) as e:
            self.state = {"ok": False, "error": f"sin conexión con Telegram ({type(e).__name__})"}
            return None
        if not j.get("ok"):
            self.state = {"ok": False, "error": str(j.get("description") or f"HTTP {getattr(r, 'status_code', '?')}")[:120],
                          "code": j.get("error_code")}
            return None
        self.state = {"ok": True, "error": None}
        return j

    def send(self, text: str, markup: dict | None = None) -> bool:
        p = {"chat_id": self.chat, "text": text, "disable_web_page_preview": True}
        if markup:
            p["reply_markup"] = markup
        return self.api("sendMessage", p) is not None

    def answer(self, cq_id: str, text: str):
        self.api("answerCallbackQuery", {"callback_query_id": cq_id, "text": text[:190]})

    def edit_markup(self, chat, msg_id, markup: dict | None):
        if msg_id is None:
            return
        self.api("editMessageReplyMarkup", {"chat_id": chat, "message_id": msg_id,
                                            "reply_markup": markup or {"inline_keyboard": []}})

    # ---------------- lectura ----------------
    def poll_once(self, timeout: int = 25) -> int:
        """Una lectura (espera hasta `timeout` s a que llegue algo). Al arrancar salta lo acumulado: un /cerrar
        escrito mientras el servicio estaba caído no debe ejecutarse horas después."""
        if self.offset is None:
            j = self.api("getUpdates", {"offset": -1, "timeout": 0}, timeout=15)
            if j is None:
                return 0
            res = j.get("result") or []
            self.offset = (res[-1]["update_id"] + 1) if res else 0
        j = self.api("getUpdates", {"offset": self.offset, "timeout": timeout,
                                    "allowed_updates": ["message", "callback_query"]}, timeout=timeout + 10)
        if j is None:
            return 0
        n = 0
        for u in j.get("result") or []:
            self.offset = max(self.offset or 0, u.get("update_id", 0) + 1)
            try:
                self.handle(u)
                n += 1
            except Exception:  # noqa: BLE001 (un mensaje raro no debe tumbar la lectura)
                log.exception("Telegram: no pude atender un mensaje")
        return n

    def forever(self):
        while True:
            try:
                self.poll_once()
            except Exception as e:  # noqa: BLE001 (el hilo no debe morir en silencio)
                log.exception("Telegram: error leyendo mensajes")
                self.state = {"ok": False, "error": f"error interno ({type(e).__name__})"}
            if not self.state.get("ok"):
                # 409: otra copia del servicio está leyendo (dura lo que tarda un redeploy)
                time.sleep(10 if self.state.get("code") == 409 else 5)

    # ---------------- atención ----------------
    def mine(self, chat, user) -> bool:
        return bool(self.chat and self.user) and str(chat) == self.chat and str(user) == self.user

    def handle(self, u: dict):
        if "callback_query" in u:
            self.on_callback(u["callback_query"])
        elif "message" in u:
            self.on_message(u["message"])

    def on_callback(self, cq: dict):
        msg = cq.get("message") or {}
        chat = (msg.get("chat") or {}).get("id")
        if not self.mine(chat, (cq.get("from") or {}).get("id")):
            self.answer(cq.get("id", ""), "No autorizado.")
            return
        data = str(cq.get("data") or "")
        if data.startswith("x:"):
            ok, txt = self.paper.pedir(data[2:])
            self.answer(cq["id"], txt)
            if ok:
                self.edit_markup(chat, msg.get("message_id"),
                                 {"inline_keyboard": [[{"text": "⏳ Enviada a paper", "callback_data": "n"}]]})
            else:
                self.send(f"✋ {txt}")
        elif data.startswith("c:si"):
            txt = self.paper.confirmar_cierre(data[5:] or None)
            self.answer(cq["id"], "Listo." if txt.startswith("🧹") else "Ese botón venció.")
            self.edit_markup(chat, msg.get("message_id"), None)
            self.send(txt)
        elif data == "c:no":
            self.answer(cq["id"], "No cambié nada.")
            self.edit_markup(chat, msg.get("message_id"), None)
        else:
            self.answer(cq.get("id", ""), "")

    def on_message(self, m: dict):
        if not self.mine((m.get("chat") or {}).get("id"), (m.get("from") or {}).get("id")):
            return
        if self.clock() - float(m.get("date") or 0) > VIEJO_S:
            return
        text = (m.get("text") or "").strip()
        if text and not text.startswith("/") and text.split()[0].lower() == "estado":
            text = "/" + text           # «estado» a secas vale lo mismo que /estado
        if self.ordenes and text:
            respuestas, atendido = self.ordenes.manejar(text)
            for r in respuestas:
                self.send(r)
            if atendido:
                return
        if not text.startswith("/"):
            return
        cmd = text.split()[0].split("@")[0]
        if cmd.lower() == "/start":
            cmd = "/ayuda"
        if self.claude and cmd.lower() == "/claude":
            txt, markup = self.claude.estado_txt(), None
        elif self.claude and cmd.lower() == "/marcador":
            txt, markup = self.claude.marcador(), None
        else:
            txt, markup = self.paper.comando(cmd)
        self.send(txt, markup)
