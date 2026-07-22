"""Konfiguration des Ida-Telegram MCP Servers, komplett über Umgebungsvariablen."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass

DEFAULT_SYSTEM_PROMPT = (
    "Du antwortest automatisch auf Telegram-Nachrichten fuer den Kontobesitzer. "
    "Antworte kurz, freundlich und hilfreich auf Deutsch. Wenn du etwas nicht "
    "sicher beantworten kannst, sag das ehrlich statt zu raten."
)


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
    anthropic_api_key: str
    claude_model: str
    claude_system_prompt: str
    claude_effort: str
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
        anthropic_api_key = (
            _require("ANTHROPIC_API_KEY") if autoreply_enabled else _optional("ANTHROPIC_API_KEY", "")
        )

        settings = Settings(
            telegram_bot_token=_require("TELEGRAM_BOT_TOKEN"),
            telegram_chat_id=_require("TELEGRAM_CHAT_ID"),
            mcp_auth_token=mcp_auth_token,
            mcp_host=_optional("MCP_HOST", "0.0.0.0"),
            mcp_port=int(_optional("MCP_PORT", "8001")),
            autoreply_enabled=autoreply_enabled,
            anthropic_api_key=anthropic_api_key,
            claude_model=_optional("CLAUDE_MODEL", "claude-opus-4-8"),
            claude_system_prompt=_optional("CLAUDE_SYSTEM_PROMPT", DEFAULT_SYSTEM_PROMPT),
            claude_effort=_optional("CLAUDE_EFFORT", "medium"),
            autoreply_debounce_seconds=int(_optional("AUTOREPLY_DEBOUNCE_SECONDS", "3")),
        )
    except ConfigError as exc:
        print(f"[Ida-Telegram] Konfigurationsfehler: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    return settings
