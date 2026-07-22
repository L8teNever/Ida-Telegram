"""Dünner Wrapper um die Telegram Bot HTTP API.

Schickt Nachrichten ausschliesslich an die in TELEGRAM_CHAT_ID konfigurierte,
feste Chat-ID -- es gibt keine Moeglichkeit, ueber diesen Server an eine
andere Person zu schreiben. Das ist bewusst so (siehe app/server.py): eine
Nachricht ist etwas, das eine echte Person erreicht und nicht rueckgaengig
gemacht werden kann, deshalb kein frei waehlbarer Empfaenger-Parameter.
"""

from __future__ import annotations

from typing import Any

import requests

from app.config import Settings

_API_BASE = "https://api.telegram.org"
_MAX_TEXT_LENGTH = 4096
_TIMEOUT_SECONDS = 15


class TelegramError(RuntimeError):
    """Fehler, die 1:1 als verständliche Meldung an Claude zurückgehen sollen."""


class TelegramClient:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def _call(self, method: str, payload: dict) -> dict[str, Any]:
        url = f"{_API_BASE}/bot{self._settings.telegram_bot_token}/{method}"
        try:
            response = requests.post(url, json=payload, timeout=_TIMEOUT_SECONDS)
        except requests.RequestException as exc:
            raise TelegramError(
                f"Verbindung zur Telegram-API fehlgeschlagen: {exc}"
            ) from exc

        try:
            data = response.json()
        except ValueError as exc:
            raise TelegramError(
                f"Telegram hat keine gueltige Antwort geliefert (HTTP {response.status_code})."
            ) from exc

        if not data.get("ok"):
            description = data.get("description", "unbekannter Fehler")
            raise TelegramError(f"Telegram-API-Fehler: {description}")

        return data["result"]

    def send_message(self, text: str) -> dict:
        text = text.strip()
        if not text:
            raise TelegramError("Nachrichtentext darf nicht leer sein.")
        if len(text) > _MAX_TEXT_LENGTH:
            raise TelegramError(
                f"Nachricht ist zu lang ({len(text)} Zeichen, Telegram erlaubt "
                f"max. {_MAX_TEXT_LENGTH})."
            )

        result = self._call(
            "sendMessage",
            {"chat_id": self._settings.telegram_chat_id, "text": text},
        )
        return {
            "gesendet": True,
            "message_id": result.get("message_id"),
            "zeitpunkt": result.get("date"),
        }

    def bot_status(self) -> dict:
        result = self._call("getMe", {})
        return {
            "bot_name": result.get("first_name"),
            "bot_username": result.get("username"),
            "erreichbar": True,
        }
