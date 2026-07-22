"""Einfacher, persistenter Notiz-Speicher fuer die Routine.

Claude Code Routinen sind zustandslos: jeder Trigger startet eine komplett
neue Sitzung ohne Kenntnis frueherer Laeufe. Damit die Routine trotzdem
"weiss", was sie bei frueheren Nachrichten schon erfahren hat, liest/schreibt
sie hierueber selbst eine kompakte, kuratierte Zusammenfassung -- kein
vollstaendiges Nachrichtenprotokoll, das mit der Zeit unkontrolliert waechst
und bei jedem Lauf unnoetig Tokens kosten wuerde. Die Kuration (was bleibt,
was wird verworfen) macht die Routine selbst.

Liegt auf einem eigenen Docker-Volume (nicht im fluechtigen /tmp), bleibt
also auch nach einem Container-Neustart erhalten.
"""

from __future__ import annotations

import threading
from pathlib import Path

_MAX_LENGTH = 20_000


class MemoryStore:
    def __init__(self, path: str) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def read(self) -> str:
        with self._lock:
            if not self._path.exists():
                return ""
            return self._path.read_text(encoding="utf-8")

    def write(self, text: str) -> str:
        text = text[:_MAX_LENGTH]
        with self._lock:
            self._path.write_text(text, encoding="utf-8")
        return text
