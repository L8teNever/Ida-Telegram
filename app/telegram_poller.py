"""Hintergrund-Loop: erkennt neue Telegram-Nachrichten der konfigurierten
Person und triggert dafuer eine claude.ai Routine -- die Routine selbst liest
die Nachricht(en) dann ueber das MCP-Tool neue_nachrichten_abrufen und
antwortet ueber nachricht_senden. Dieser Container ruft NICHT selbst die
Claude-API auf; er meldet nur "es gibt Neues" und haelt die Nachrichten
zwischengespeichert, bis die Routine sie abholt.

Unterstuetzt Text (jede Formatierung -- Telegram liefert reinen Text ohne
Markup, Formatierung aendert daran nichts), Fotos (werden heruntergeladen und
als echtes Bild an neue_nachrichten_abrufen weitergegeben, die Routine kann
sie also wirklich "sehen") und Sprachnachrichten (werden erkannt und lösen
einen Trigger aus, aber NICHT automatisch transkribiert -- Claude hat keine
native Audio-Eingabe, dafuer waere ein separater Speech-to-Text-Dienst noetig).

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
from typing import Any

import requests

from app.config import Settings
from app.telegram_client import TelegramClient

log = logging.getLogger("ida-telegram.autoreply")

_TELEGRAM_API_BASE = "https://api.telegram.org"
_TELEGRAM_FILE_BASE = "https://api.telegram.org/file"
_LONG_POLL_TIMEOUT = 30
_ERROR_BACKOFF_SECONDS = 5
_TRIGGER_TIMEOUT_SECONDS = 15
_FILE_DOWNLOAD_TIMEOUT_SECONDS = 30

# https://platform.claude.com/docs/en/api/claude-code/routines-fire
_ROUTINE_FIRE_URL = "https://api.anthropic.com/v1/claude_code/routines/{routine_id}/fire"
_ROUTINE_BETA_HEADER = "experimental-cc-routine-2026-04-01"
_ANTHROPIC_VERSION = "2023-06-01"
_ROUTINE_TEXT_MAX_LENGTH = 65536


def _summarize(entries: list[dict[str, Any]]) -> str:
    """Reine Textzusammenfassung fuer das 'text'-Feld beim Routine-Trigger --
    dient nur als sofortiger Kontext-Hinweis, die eigentlichen Bilddaten
    liefert erst neue_nachrichten_abrufen."""
    parts = []
    for entry in entries:
        if entry["kind"] == "text":
            parts.append(entry["text"])
        elif entry["kind"] == "photo":
            parts.append(f"[Foto]{' ' + entry['caption'] if entry.get('caption') else ''}")
    return "\n".join(parts)


class TelegramPoller:
    def __init__(self, settings: Settings, telegram: TelegramClient) -> None:
        self._settings = settings
        self._telegram = telegram
        self._offset = 0

        self._pending_lock = threading.Lock()
        self._pending_entries: list[dict[str, Any]] = []

    def pending_entries(self) -> list[dict[str, Any]]:
        """Vom MCP-Tool neue_nachrichten_abrufen aufgerufen: gibt die
        Eintraege zurueck, die den aktuellen Lauf ausgeloest haben, und leert
        den Zwischenspeicher -- jeder Eintrag wird nur einmal ausgeliefert."""
        with self._pending_lock:
            entries, self._pending_entries = self._pending_entries, []
            return entries

    def _get_updates(self, timeout: int) -> list[dict]:
        url = f"{_TELEGRAM_API_BASE}/bot{self._settings.telegram_bot_token}/getUpdates"
        response = requests.get(
            url,
            params={"offset": self._offset, "timeout": timeout, "allowed_updates": ["message"]},
            timeout=timeout + 10,
        )
        data = response.json()
        if not data.get("ok"):
            raise RuntimeError(data.get("description", "unbekannter Fehler"))
        return data["result"]

    def _download_file(self, file_id: str) -> bytes:
        get_file_url = f"{_TELEGRAM_API_BASE}/bot{self._settings.telegram_bot_token}/getFile"
        info = requests.get(
            get_file_url, params={"file_id": file_id}, timeout=_FILE_DOWNLOAD_TIMEOUT_SECONDS
        ).json()
        if not info.get("ok"):
            raise RuntimeError(info.get("description", "getFile fehlgeschlagen"))
        file_path = info["result"]["file_path"]

        download_url = f"{_TELEGRAM_FILE_BASE}/bot{self._settings.telegram_bot_token}/{file_path}"
        response = requests.get(download_url, timeout=_FILE_DOWNLOAD_TIMEOUT_SECONDS)
        response.raise_for_status()
        return response.content

    def _entries_from(self, updates: list[dict]) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        for update in updates:
            # Offset sofort weiterschieben -- auch fuer Updates, die wir
            # ignorieren (fremde chat_id, nicht unterstuetzte Typen). Sonst
            # wuerde Telegram sie beim naechsten Poll erneut liefern.
            self._offset = max(self._offset, update["update_id"] + 1)

            message = update.get("message") or {}
            chat = message.get("chat") or {}
            if str(chat.get("id")) != str(self._settings.telegram_chat_id):
                continue

            text = message.get("text")
            caption = message.get("caption")
            photo = message.get("photo")
            voice = message.get("voice")

            if text:
                entries.append({"kind": "text", "text": text})
            elif photo:
                # Telegram liefert mehrere Aufloesungen, letzte = groesste.
                largest = photo[-1]
                try:
                    data = self._download_file(largest["file_id"])
                    entries.append({"kind": "photo", "data": data, "caption": caption})
                except Exception:
                    log.exception("Foto-Download fehlgeschlagen")
                    hinweis = "[Foto konnte nicht heruntergeladen werden]"
                    if caption:
                        hinweis += f" Bildunterschrift: {caption}"
                    entries.append({"kind": "text", "text": hinweis})
            elif voice:
                duration = voice.get("duration", 0)
                hinweis = (
                    f"[Sprachnachricht erhalten, {duration}s -- automatische "
                    "Transkription ist nicht eingerichtet, der Inhalt ist "
                    "nicht bekannt]"
                )
                if caption:
                    hinweis += f" Bildunterschrift: {caption}"
                entries.append({"kind": "text", "text": hinweis})
            # Andere Typen (Sticker, Dokumente, Videos, ...) werden aktuell
            # ignoriert.
        return entries

    def _collect_batch(self, first_entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        buffer = list(first_entries)
        while True:
            more = self._get_updates(timeout=self._settings.autoreply_debounce_seconds)
            more_entries = self._entries_from(more)
            if not more_entries:
                break
            buffer.extend(more_entries)
        return buffer

    def _trigger_routine(self, text: str) -> None:
        url = _ROUTINE_FIRE_URL.format(routine_id=self._settings.routine_id)
        try:
            response = requests.post(
                url,
                headers={
                    "Authorization": f"Bearer {self._settings.routine_api_key}",
                    "anthropic-beta": _ROUTINE_BETA_HEADER,
                    "anthropic-version": _ANTHROPIC_VERSION,
                    "Content-Type": "application/json",
                },
                json={"text": text[:_ROUTINE_TEXT_MAX_LENGTH]},
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
                entries = self._entries_from(updates)
                if not entries:
                    continue

                batch = self._collect_batch(entries)
                with self._pending_lock:
                    self._pending_entries.extend(batch)

                log.info("Neue Nachricht(en) erhalten, triggere Routine...")
                # "text" wird zusaetzlich zum Puffer mitgeschickt -- gibt der
                # Routine sofortigen Kontext, ohne dass sie zwingend erst
                # neue_nachrichten_abrufen aufrufen muss. Bilddaten selbst
                # gehen nur ueber neue_nachrichten_abrufen raus.
                self._trigger_routine(_summarize(batch))
            except Exception:
                log.exception("Fehler im Telegram-Poll-Loop, versuche es weiter")
                time.sleep(_ERROR_BACKOFF_SECONDS)


def start_background(poller: TelegramPoller) -> None:
    thread = threading.Thread(target=poller.run, name="telegram-autoreply", daemon=True)
    thread.start()
