"""Hintergrund-Loop: liest neue Telegram-Nachrichten der konfigurierten Person
und laesst Claude automatisch antworten.

Nutzt Telegram long-polling (getUpdates) statt eines Webhooks: kein neuer
oeffentlicher Endpunkt noetig -- der Container macht nur ausgehende HTTPS-
Verbindungen zu api.telegram.org, genau wie schon fuer nachricht_senden.
Telegrams eigener offset-Mechanismus sorgt von selbst dafuer, dass jedes
Update genau einmal verarbeitet wird (keine eigene Dedupe-Logik noetig).

Schnell hintereinander geschickte Nachrichten werden gebuendelt: nach der
ersten neuen Nachricht wird kurz (AUTOREPLY_DEBOUNCE_SECONDS) weiter auf
Nachschub gewartet -- dieses Warten IST der naechste long-poll-Aufruf, keine
zusaetzliche Sleep-Schleife. Kommt in der Zeit nichts mehr, wird der ganze
gesammelte Block in einer Antwort beantwortet.
"""

from __future__ import annotations

import logging
import threading
import time

import requests

from app.claude_client import ClaudeClient
from app.config import Settings
from app.telegram_client import TelegramClient

log = logging.getLogger("ida-telegram.autoreply")

_API_BASE = "https://api.telegram.org"
_LONG_POLL_TIMEOUT = 30
_ERROR_BACKOFF_SECONDS = 5


class TelegramPoller:
    def __init__(self, settings: Settings, telegram: TelegramClient, claude: ClaudeClient) -> None:
        self._settings = settings
        self._telegram = telegram
        self._claude = claude
        self._offset = 0

    def _get_updates(self, timeout: int) -> list[dict]:
        url = f"{_API_BASE}/bot{self._settings.telegram_bot_token}/getUpdates"
        response = requests.get(
            url,
            params={"offset": self._offset, "timeout": timeout, "allowed_updates": ["message"]},
            timeout=timeout + 10,
        )
        data = response.json()
        if not data.get("ok"):
            raise RuntimeError(data.get("description", "unbekannter Fehler"))
        return data["result"]

    def _texts_from(self, updates: list[dict]) -> list[str]:
        texts = []
        for update in updates:
            # Offset sofort weiterschieben -- auch fuer Updates, die wir
            # ignorieren (fremde chat_id, Nicht-Text-Nachrichten). Sonst
            # wuerde Telegram sie beim naechsten Poll erneut liefern.
            self._offset = max(self._offset, update["update_id"] + 1)

            message = update.get("message") or {}
            chat = message.get("chat") or {}
            if str(chat.get("id")) != str(self._settings.telegram_chat_id):
                continue

            text = message.get("text")
            if text:
                texts.append(text)
        return texts

    def _collect_batch(self, first_texts: list[str]) -> str:
        buffer = list(first_texts)
        while True:
            more = self._get_updates(timeout=self._settings.autoreply_debounce_seconds)
            more_texts = self._texts_from(more)
            if not more_texts:
                break
            buffer.extend(more_texts)
        return "\n".join(buffer)

    def run(self) -> None:
        log.info(
            "Telegram-Autoreply-Loop gestartet (Modell: %s, Debounce: %ss)",
            self._settings.claude_model,
            self._settings.autoreply_debounce_seconds,
        )
        while True:
            try:
                updates = self._get_updates(timeout=_LONG_POLL_TIMEOUT)
                texts = self._texts_from(updates)
                if not texts:
                    continue

                combined = self._collect_batch(texts)
                log.info("Neue Nachricht(en) erhalten, frage Claude...")

                try:
                    antwort = self._claude.antworten(combined)
                except Exception:
                    log.exception("Claude-Anfrage fehlgeschlagen")
                    antwort = "Entschuldige, da ist gerade ein Fehler bei mir passiert."

                self._telegram.send_message(antwort)
            except Exception:
                log.exception("Fehler im Telegram-Poll-Loop, versuche es weiter")
                time.sleep(_ERROR_BACKOFF_SECONDS)


def start_background(settings: Settings, telegram: TelegramClient, claude: ClaudeClient) -> None:
    poller = TelegramPoller(settings, telegram, claude)
    thread = threading.Thread(target=poller.run, name="telegram-autoreply", daemon=True)
    thread.start()
