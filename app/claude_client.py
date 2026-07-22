"""Wrapper um die Anthropic Messages API fuer automatische Telegram-Antworten."""

from __future__ import annotations

import anthropic

from app.config import Settings

_MAX_TOKENS = 1024


class ClaudeClient:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

    def antworten(self, nachricht: str) -> str:
        response = self._client.messages.create(
            model=self._settings.claude_model,
            max_tokens=_MAX_TOKENS,
            system=self._settings.claude_system_prompt,
            thinking={"type": "adaptive"},
            output_config={"effort": self._settings.claude_effort},
            messages=[{"role": "user", "content": nachricht}],
        )

        if response.stop_reason == "refusal":
            return "Dazu kann ich gerade nichts sagen."

        text = "".join(block.text for block in response.content if block.type == "text")
        return text.strip() or "(keine Antwort erhalten)"
