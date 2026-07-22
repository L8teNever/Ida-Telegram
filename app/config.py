"""Konfiguration des Ida-Telegram MCP Servers, komplett über Umgebungsvariablen."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass


class ConfigError(RuntimeError):
    pass


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(f"Umgebungsvariable {name} fehlt oder ist leer.")
    return value


def _optional(name: str, default: str) -> str:
    value = os.environ.get(name, "").strip()
    return value or default


def _optional_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name, "").strip().lower()
    if not value:
        return default
    return value in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    telegram_chat_id: str

    mcp_auth_token: str
    mcp_host: str
    mcp_port: int

    autoreply_enabled: bool
    routine_id: str
    routine_api_key: str
    autoreply_debounce_seconds: int


def load_settings() -> Settings:
    try:
        mcp_auth_token = _require("MCP_AUTH_TOKEN")
        if len(mcp_auth_token) < 16:
            raise ConfigError(
                "MCP_AUTH_TOKEN ist zu kurz (mind. 16 Zeichen). "
                "Erzeuge z.B. mit: openssl rand -hex 32"
            )

        autoreply_enabled = _optional_bool("AUTOREPLY_ENABLED", True)
        routine_id = _require("ROUTINE_ID") if autoreply_enabled else _optional("ROUTINE_ID", "")
        routine_api_key = (
            _require("ROUTINE_API_KEY") if autoreply_enabled else _optional("ROUTINE_API_KEY", "")
        )

        settings = Settings(
            telegram_bot_token=_require("TELEGRAM_BOT_TOKEN"),
            telegram_chat_id=_require("TELEGRAM_CHAT_ID"),
            mcp_auth_token=mcp_auth_token,
            mcp_host=_optional("MCP_HOST", "0.0.0.0"),
            mcp_port=int(_optional("MCP_PORT", "8001")),
            autoreply_enabled=autoreply_enabled,
            routine_id=routine_id,
            routine_api_key=routine_api_key,
            autoreply_debounce_seconds=int(_optional("AUTOREPLY_DEBOUNCE_SECONDS", "3")),
        )
    except ConfigError as exc:
        print(f"[Ida-Telegram] Konfigurationsfehler: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    return settings
