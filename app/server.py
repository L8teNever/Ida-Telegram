"""Ida-Telegram MCP Server.

Stellt genau einer, in TELEGRAM_CHAT_ID fest konfigurierten Person eine
Telegram-Nachricht als MCP-Tool fuer Claude bereit -- ueber Streamable HTTP,
damit es per Remote-MCP-Verbindung (z.B. ueber einen Cloudflare Tunnel)
genutzt werden kann. Der Endpunkt ist per Shared-Secret-Token abgesichert
(siehe app/auth.py). Absichtlich kein Parameter fuer eine andere chat_id --
dieser Server kann strukturell nur an die eine konfigurierte Person schreiben.

Wenn AUTOREPLY_ENABLED=true erkennt ein Hintergrund-Loop (app/telegram_poller.py)
neue Telegram-Nachrichten und triggert dafuer eine claude.ai Routine ueber
deren eigenen API-Token (ROUTINE_TRIGGER_URL/ROUTINE_API_KEY). Der Container
ruft dabei selbst keine Claude-API auf -- die Routine liest die Nachricht(en)
ueber das Tool neue_nachrichten_abrufen und antwortet ueber nachricht_senden.
"""

from __future__ import annotations

import logging

import uvicorn
from mcp.server.fastmcp import FastMCP
from starlette.responses import JSONResponse

from app.auth import BearerAuthMiddleware
from app.config import load_settings
from app.telegram_client import TelegramClient
from app.telegram_poller import TelegramPoller, start_background

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
log = logging.getLogger("ida-telegram")

settings = load_settings()
client = TelegramClient(settings)
poller = TelegramPoller(settings, client) if settings.autoreply_enabled else None

mcp = FastMCP(
    "Ida-Telegram",
    instructions=(
        "Zwei Werkzeuge fuer die eine fest konfigurierte Person: "
        "neue_nachrichten_abrufen liest, was sie gerade geschrieben hat, "
        "nachricht_senden schickt eine Antwort. Es gibt keinen Empfaenger-"
        "Parameter -- beide Tools betreffen immer nur die in TELEGRAM_CHAT_ID "
        "hinterlegte Person."
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
def neue_nachrichten_abrufen() -> dict:
    """Gibt die Telegram-Nachricht(en) zurueck, die diesen Lauf ausgeloest haben.

    Jede Nachricht wird nur einmal ausgeliefert (Zwischenspeicher wird beim
    Abrufen geleert). Leere Liste, wenn AUTOREPLY_ENABLED=false ist oder
    gerade keine neue Nachricht ansteht.
    """
    if poller is None:
        return {"nachrichten": []}
    return {"nachrichten": poller.pending_messages()}


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
    if poller is not None:
        start_background(poller)
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
