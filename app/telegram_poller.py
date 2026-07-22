"""Hintergrund-Loop: erkennt neue Telegram-Nachrichten der konfigurierten
Person und triggert dafuer eine claude.ai Routine -- die Routine selbst liest
die Nachricht(en) dann ueber das MCP-Tool neue_nachrichten_abrufen und
antwortet ueber nachricht_senden. Dieser Container ruft NICHT selbst die
Claude-API auf; er meldet nur "es gibt Neues" und haelt die Nachrichten
zwischengespeichert, bis die Routine sie abholt.

Nutzt Telegram long-polling (getUpdates) statt eines Webhooks: kein neuer
oeffentlicher Endpunkt noetig -- der Container macht nur ausgehende HTTPS-
Verbindungen zu api.telegram.org und zur Routine-Trigger-URL. Telegrams
eigener offset-Mechanismus sorgt von selbst dafuer, dass jedes Update genau
einmal verarbeitet wird (keine eigene Dedupe-Logik noetig).

Schnell hintereinander geschickte Nachrichten werden gebuendelt: nach der
ersten neuen Nachricht wird kurz (AUTOREPLY_DEBOUNCE_SECONDS) weiter auf
Nachschub gewartet -- dieses Warten IST der naechste long-poll-Aufruf, keine
zusaetzliche Sleep-Schleife. Kommt in der Zeit nichts mehr, wird die Routine
genau einmal getriggert statt einmal pro Nachricht.
"""

from __future__ import annotations

import logging
import threading
import time

import requests

from app.config import Settings
from app.telegram_client import TelegramClient

log = logging.getLogger("ida-telegram.autoreply")

_API_BASE = "https://api.telegram.org"
_LONG_POLL_TIMEOUT = 30
_ERROR_BACKOFF_SECONDS = 5
_TRIGGER_TIMEOUT_SECONDS = 15


class TelegramPoller:
    def __init__(self, settings: Settings, telegram: TelegramClient) -> None:
        self._settings = settings
        self._telegram = telegram
        self._offset = 0

        self._pending_lock = threading.Lock()
        self._pending_messages: list[str] = []

    def pending_messages(self) -> list[str]:
        """Vom MCP-Tool neue_nachrichten_abrufen aufgerufen: gibt die
        Nachrichten zurueck, die den aktuellen Lauf ausgeloest haben, und
        leert den Zwischenspeicher -- jede Nachricht wird nur einmal
        ausgeliefert."""
        with self._pending_lock:
            messages, self._pending_messages = self._pending_messages, []
            return messages

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

    def _collect_batch(self, first_texts: list[str]) -> list[str]:
        buffer = list(first_texts)
        while True:
            more = self._get_updates(timeout=self._settings.autoreply_debounce_seconds)
            more_texts = self._texts_from(more)
            if not more_texts:
                break
            buffer.extend(more_texts)
        return buffer

    def _trigger_routine(self) -> None:
        try:
            response = requests.post(
                self._settings.routine_trigger_url,
                headers={"Authorization": f"Bearer {self._settings.routine_api_key}"},
                timeout=_TRIGGER_TIMEOUT_SECONDS,
            )
            if response.status_code >= 300:
                log.error(
                    "Routine-Trigger fehlgeschlagen: HTTP %s -- %s",
                    response.status_code,
                    response.text[:500],
                )
        except requests.RequestException:
            log.exception("Routine-Trigger fehlgeschlagen (Netzwerkfehler)")

    def run(self) -> None:
        log.info(
            "Telegram-Autoreply-Loop gestartet (Debounce: %ss)",
            self._settings.autoreply_debounce_seconds,
        )
        while True:
            try:
                updates = self._get_updates(timeout=_LONG_POLL_TIMEOUT)
                texts = self._texts_from(updates)
                if not texts:
                    continue

                batch = self._collect_batch(texts)
                with self._pending_lock:
                    self._pending_messages.extend(batch)

                log.info("Neue Nachricht(en) erhalten, triggere Routine...")
                self._trigger_routine()
            except Exception:
                log.exception("Fehler im Telegram-Poll-Loop, versuche es weiter")
                time.sleep(_ERROR_BACKOFF_SECONDS)


def start_background(poller: TelegramPoller) -> None:
    thread = threading.Thread(target=poller.run, name="telegram-autoreply", daemon=True)
    thread.start()
