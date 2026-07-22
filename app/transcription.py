"""Lokale Sprachnachrichten-Transkription per faster-whisper (CTranslate2) --
laeuft direkt in diesem Container, keine Audiodaten verlassen die eigene
Infrastruktur. faster-whisper statt des originalen openai-whisper/PyTorch:
deutlich schnellere und speicherschonendere CPU-Inferenz, wichtig auf einem
VPS ohne GPU. Decodiert komprimierte Formate wie Telegrams OGG/Opus selbst
(ueber die mitgelieferte av/FFmpeg-Anbindung), keine eigene Vorkonvertierung
noetig.

Das Modell wird ERST beim ersten tatsaechlichen Transkriptions-Aufruf
geladen (nicht beim Container-Start) -- sonst wuerde jeder Start auf den
teils mehrminuetigen Modell-Download/-Ladevorgang warten muessen, auch wenn
gar keine Sprachnachricht ankommt. Danach bleibt es fuer die Laufzeit des
Containers im Speicher (kein wiederholtes Neuladen pro Aufruf).
"""

from __future__ import annotations

import logging
import os
import secrets
import threading

from app.config import Settings

log = logging.getLogger("ida-telegram.transcription")

_model = None
_model_lock = threading.Lock()


class TranscriptionError(RuntimeError):
    """Fehler, die 1:1 als verständliche Meldung an Claude zurückgehen sollen."""


def _get_model(settings: Settings):
    global _model
    if _model is not None:
        return _model

    with _model_lock:
        if _model is None:
            from faster_whisper import WhisperModel

            log.info(
                "Lade Whisper-Modell '%s' (compute_type=%s) -- beim allerersten "
                "Mal wird es von Hugging Face heruntergeladen, das kann ein "
                "paar Minuten dauern...",
                settings.whisper_model,
                settings.whisper_compute_type,
            )
            _model = WhisperModel(
                settings.whisper_model,
                device="cpu",
                compute_type=settings.whisper_compute_type,
                download_root=settings.whisper_model_cache_dir,
            )
            log.info("Whisper-Modell geladen.")
    return _model


def transcribe_audio(audio_bytes: bytes, settings: Settings) -> str:
    """Transkribiert rohe Audiodaten (z.B. eine heruntergeladene Telegram-
    Sprachnachricht im OGG/Opus-Format) zu Text.

    Ueber eine temporaere Datei statt In-Memory-Puffer, weil faster-whisper
    (bzw. die darunterliegende av/FFmpeg-Dekodierung) einen Dateipfad
    braucht, um das Format selbst zu erkennen."""
    model = _get_model(settings)

    tmp_path = os.path.join("/tmp", f"{secrets.token_hex(8)}.ogg")
    try:
        with open(tmp_path, "wb") as f:
            f.write(audio_bytes)
        language = settings.whisper_language or None
        segments, _info = model.transcribe(tmp_path, language=language)
        text = "".join(segment.text for segment in segments).strip()
    except Exception as exc:
        log.exception("Transkription fehlgeschlagen")
        raise TranscriptionError(f"Transkription fehlgeschlagen: {exc}") from exc
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass

    return text or "(Whisper konnte keinen Text erkennen -- evtl. zu leise, zu kurz, oder kein Sprachinhalt)"
