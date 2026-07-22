"""Ida-Telegram MCP Server.

Stellt genau einer, in TELEGRAM_CHAT_ID fest konfigurierten Person eine
Telegram-Nachricht als MCP-Tool fuer Claude bereit -- ueber Streamable HTTP,
damit es per Remote-MCP-Verbindung (z.B. ueber einen Cloudflare Tunnel)
genutzt werden kann. Der Endpunkt ist per Shared-Secret-Token abgesichert
(siehe app/auth.py). Absichtlich kein Parameter fuer eine andere chat_id --
dieser Server kann strukturell nur an die eine konfigurierte Person schreiben.
"""

from __future__ import annotations

import logging

import uvicorn
from mcp.server.fastmcp import FastMCP
from starlette.responses import JSONResponse

from app.auth import BearerAuthMiddleware
from app.config import load_settings
from app.telegram_client import TelegramClient

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
log = logging.getLogger("ida-telegram")

settings = load_settings()
client = TelegramClient(settings)

if settings.autoreply_enabled:
    from app.claude_client import ClaudeClient
    from app.telegram_poller import start_background

    _claude_client = ClaudeClient(settings)
else:
    _claude_client = None

mcp = FastMCP(
    "Ida-Telegram",
    instructions=(
        "Werkzeug, um der einen fest konfigurierten Person eine Telegram-"
        "Nachricht zu schicken. Es gibt keinen Empfaenger-Parameter -- jede "
        "Nachricht geht an genau die in TELEGRAM_CHAT_ID hinterlegte Person."
    ),
    host=settings.mcp_host,
    port=settings.mcp_port,
)


@mcp.tool()
def nachricht_senden(text: str) -> dict:
    """Sendet eine Textnachricht an die fest konfigurierte Person via Telegram.

    text: Nachrichtentext (max. 4096 Zeichen, reiner Text ohne Markdown-Formatierung).
    Gibt bei Erfolg message_id und Zeitpunkt zurueck.
    """
    return client.send_message(text)


@mcp.tool()
def bot_status() -> dict:
    """Prueft, ob der Telegram-Bot-Token gueltig und die Bot-API erreichbar ist.

    Sendet keine Nachricht, nur ein Verbindungstest (Telegrams getMe).
    """
    return client.bot_status()


async def healthz(request):
    return JSONResponse({"status": "ok"})


def build_app():
    app = mcp.streamable_http_app()
    app.add_route("/healthz", healthz, methods=["GET"])
    app.add_middleware(BearerAuthMiddleware, token=settings.mcp_auth_token)
    return app


def main() -> None:
    app = build_app()
    log.info(
        "Ida-Telegram MCP Server startet auf %s:%s (Endpunkt: /mcp, Health: /healthz)",
        settings.mcp_host,
        settings.mcp_port,
    )
    if settings.autoreply_enabled:
        start_background(settings, client, _claude_client)
    else:
        log.info("AUTOREPLY_ENABLED=false -- automatisches Antworten ist deaktiviert.")
    # access_log=False: uvicorn wuerde sonst jede Request-Zeile inkl. vollem
    # Pfad loggen -- und damit ein per ?token= mitgeschicktes MCP_AUTH_TOKEN
    # im Klartext in die Docker-Logs schreiben.
    uvicorn.run(
        app,
        host=settings.mcp_host,
        port=settings.mcp_port,
        log_level="info",
        access_log=False,
    )


if __name__ == "__main__":
    main()
