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


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    telegram_chat_id: str

    mcp_auth_token: str
    mcp_host: str
    mcp_port: int


def load_settings() -> Settings:
    try:
        mcp_auth_token = _require("MCP_AUTH_TOKEN")
        if len(mcp_auth_token) < 16:
            raise ConfigError(
                "MCP_AUTH_TOKEN ist zu kurz (mind. 16 Zeichen). "
                "Erzeuge z.B. mit: openssl rand -hex 32"
            )

        settings = Settings(
            telegram_bot_token=_require("TELEGRAM_BOT_TOKEN"),
            telegram_chat_id=_require("TELEGRAM_CHAT_ID"),
            mcp_auth_token=mcp_auth_token,
            mcp_host=_optional("MCP_HOST", "0.0.0.0"),
            mcp_port=int(_optional("MCP_PORT", "8001")),
        )
    except ConfigError as exc:
        print(f"[Ida-Telegram] Konfigurationsfehler: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    return settings
