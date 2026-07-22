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

Sobald eine Nachricht erkannt wird, zeigt ein eigener Hintergrund-Thread
Telegrams "tippt..."-Status an, bis die Antwort tatsaechlich rausgeht (oder
eine Sicherheitsobergrenze erreicht ist). Telegrams sendChatAction laeuft
laut offizieller Doku nach hoechstens 5 Sekunden von selbst ab, deshalb wird
er alle _TYPING_REPEAT_SECONDS (< 5s) ohne Pause neu gesendet -- eine Pause
wuerde die Anzeige sichtbar verschwinden lassen. app/server.py ruft beim
tatsaechlichen Versand (nachricht_senden) notify_reply_sent() auf, das
stoppt die Schleife sofort.

Zusaetzlich zu neue_nachrichten_abrufen (liefert jeden Eintrag nur EINMAL,
das loest den Routine-Trigger aus) haelt der Poller einen rollierenden
Kurzverlauf der letzten CHAT_HISTORY_LENGTH Nachrichten -- eingehend UND
ausgehend, ueber das Tool chat_verlauf beliebig oft abrufbar (nicht
destruktiv). Telegrams Bot-API selbst bietet keine Moeglichkeit, alte
Nachrichten nachtraeglich abzufragen (kein "getChatHistory" fuer Bots) --
deshalb wird der Verlauf hier selbst mitgeschrieben, sobald Nachrichten den
Server durchlaufen. Fotos stehen dort nur als Platzhaltertext (z.B.
"[Foto]"), nicht als echte Bilddaten -- die gibt es weiterhin nur einmalig
ueber neue_nachrichten_abrufen, sonst wuerde jeder chat_verlauf-Aufruf
unnoetig viele Tokens fuer laengst gesehene Bilder verbrauchen.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Any

import requests

from app.config import Settings
from app.telegram_client import TelegramClient

log = logging.getLogger("ida-telegram.autoreply")

_HISTORY_TEXT_MAX_LENGTH = 500

_TELEGRAM_API_BASE = "https://api.telegram.org"
_TELEGRAM_FILE_BASE = "https://api.telegram.org/file"
_LONG_POLL_TIMEOUT = 30
_ERROR_BACKOFF_SECONDS = 5
_TRIGGER_TIMEOUT_SECONDS = 15
_FILE_DOWNLOAD_TIMEOUT_SECONDS = 30

# Telegrams "tippt..."-Status laeuft laut Bot-API-Doku nach hoechstens 5s ab
# ("The status is set for 5 seconds or less") -- deshalb deutlich darunter
# neu senden, sonst blinkt die Anzeige sichtbar aus und wieder ein.
_TYPING_REPEAT_SECONDS = 4
# Sicherheitsobergrenze, falls die Routine nie antwortet (Fehler, Timeout) --
# damit "tippt..." nicht ewig weiterlaeuft, sondern irgendwann von selbst
# aufhoert, wenn nichts mehr passiert.
_TYPING_MAX_SECONDS = 300

# https://platform.claude.com/docs/en/api/claude-code/routines-fire
_ROUTINE_FIRE_URL = "https://api.anthropic.com/v1/claude_code/routines/{routine_id}/fire"
_ROUTINE_BETA_HEADER = "experimental-cc-routine-2026-04-01"
_ANTHROPIC_VERSION = "2023-06-01"
_ROUTINE_TEXT_MAX_LENGTH = 65536


def _entry_text(entry: dict[str, Any]) -> str:
    """Text-Darstellung eines einzelnen Eintrags -- fuer den Routine-Trigger-Text
    und den rollierenden chat_verlauf. Fotos nur als Platzhalter, nie als
    Bilddaten (die gibt es weiterhin nur einmalig ueber neue_nachrichten_abrufen)."""
    if entry["kind"] == "photo":
        return f"[Foto]{' ' + entry['caption'] if entry.get('caption') else ''}"
    return entry["text"]


def _summarize(entries: list[dict[str, Any]]) -> str:
    """Reine Textzusammenfassung fuer das 'text'-Feld beim Routine-Trigger --
    dient nur als sofortiger Kontext-Hinweis, die eigentlichen Bilddaten
    liefert erst neue_nachrichten_abrufen."""
    return "\n".join(_entry_text(e) for e in entries)


class TelegramPoller:
    def __init__(self, settings: Settings, telegram: TelegramClient) -> None:
        self._settings = settings
        self._telegram = telegram
        self._offset = 0

        self._pending_lock = threading.Lock()
        self._pending_entries: list[dict[str, Any]] = []

        self._typing_lock = threading.Lock()
        self._typing_stop_event: threading.Event | None = None

        self._history_lock = threading.Lock()
        self._history: deque[dict[str, str]] = deque(maxlen=settings.chat_history_length)

    def chat_verlauf(self) -> list[dict[str, str]]:
        """Vom MCP-Tool chat_verlauf aufgerufen: nicht-destruktiver, rollierender
        Kurzverlauf der letzten chat_history_length Nachrichten (eingehend UND
        ausgehend) fuer zusaetzlichen Kontext -- unabhaengig von
        pending_entries(), beliebig oft abrufbar, liefert also nicht nur
        einmalig aus."""
        with self._history_lock:
            return list(self._history)

    def record_outgoing(self, text: str) -> None:
        """Von app.server.nachricht_senden aufgerufen, damit chat_verlauf auch
        die eigenen Antworten zeigt, nicht nur eingehende Nachrichten."""
        with self._history_lock:
            self._history.append({"richtung": "ausgehend", "text": text[:_HISTORY_TEXT_MAX_LENGTH]})

    def _record_incoming(self, entries: list[dict[str, Any]]) -> None:
        with self._history_lock:
            for entry in entries:
                text = _entry_text(entry)[:_HISTORY_TEXT_MAX_LENGTH]
                self._history.append({"richtung": "eingehend", "text": text})

    def notify_reply_sent(self) -> None:
        """Von app.server.nachricht_senden aufgerufen, sobald tatsaechlich
        eine Antwort rausgegangen ist -- stoppt die laufende
        "tippt..."-Anzeige sofort, statt auf die Sicherheitsobergrenze zu
        warten."""
        with self._typing_lock:
            if self._typing_stop_event is not None:
                self._typing_stop_event.set()

    def _keep_typing_until_reply(self) -> None:
        stop_event = threading.Event()
        with self._typing_lock:
            self._typing_stop_event = stop_event
        try:
            deadline = time.monotonic() + _TYPING_MAX_SECONDS
            while time.monotonic() < deadline:
                try:
                    self._telegram.send_chat_action("typing")
                except Exception:
                    log.exception("Tipp-Anzeige (sendChatAction) fehlgeschlagen")
                # wait() statt sleep(): wacht sofort auf, wenn notify_reply_sent()
                # zwischendurch feuert, statt bis zum naechsten Intervall zu warten.
                if stop_event.wait(timeout=_TYPING_REPEAT_SECONDS):
                    break
        finally:
            with self._typing_lock:
                if self._typing_stop_event is stop_event:
                    self._typing_stop_event = None

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

                # Ab hier "tippt..." anzeigen -- schon waehrend des
                # Debounce-Wartens unten, nicht erst beim eigentlichen
                # Routine-Trigger, damit es ab dem Erkennen der Nachricht
                # durchgehend sichtbar ist.
                threading.Thread(
                    target=self._keep_typing_until_reply,
                    name="telegram-typing",
                    daemon=True,
                ).start()

                batch = self._collect_batch(entries)
                self._record_incoming(batch)
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
