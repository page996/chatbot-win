from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("audio")
    parser.add_argument("--model", default="base")
    parser.add_argument("--language", default="auto")
    args = parser.parse_args()
    path = Path(args.audio)
    language = str(args.language or "auto").strip().lower()
    whisper_payload = _try_faster_whisper(path, args.model, language=language)
    if whisper_payload.get("ok") or not _should_fallback_to_pocketsphinx(str(whisper_payload.get("error", ""))):
        payload = whisper_payload
    else:
        if language in {"en", "en-us", "english"}:
            payload = _try_pocketsphinx(
                path,
                args.model,
                whisper_error=str(whisper_payload.get("error", "")),
                sapi_error="windows_sapi_not_used",
            )
        elif _env_enabled("CHATBOT_WIN_ENABLE_WINDOWS_SAPI_ASR"):
            payload = _try_windows_sapi(path, args.model, whisper_error=str(whisper_payload.get("error", "")))
        else:
            payload = {
                "ok": False,
                "backend": "windows_sapi",
                "model": "installed_recognizer",
                "language": "",
                "fallback_from": "faster_whisper",
                "fallback_error": str(whisper_payload.get("error", "")),
                "error": "windows_sapi_fallback_disabled",
                "text_b64": "",
            }
    print("LOCAL_ASR_JSON:" + json.dumps(payload, ensure_ascii=True))
    return 0


def _try_faster_whisper(path: Path, model: str, *, language: str) -> dict[str, object]:
    try:
        from faster_whisper import WhisperModel

        whisper = WhisperModel(model, device="auto", compute_type="auto")
        language_hint = None if language in {"", "auto"} else language
        segments, info = whisper.transcribe(str(path), vad_filter=True, language=language_hint)
        text = "\n".join(segment.text.strip() for segment in segments if segment.text.strip())
        return {
            "ok": bool(text.strip()),
            "backend": "faster_whisper",
            "model": model,
            "language": getattr(info, "language", "") or "",
            "text_b64": base64.b64encode(text.encode("utf-8")).decode("ascii"),
        }
    except Exception as exc:
        return {
            "ok": False,
            "backend": "faster_whisper",
            "model": model,
            "error": f"{type(exc).__name__}: {exc}",
            "text_b64": "",
        }


def _env_enabled(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _try_windows_sapi(path: Path, model: str, *, whisper_error: str) -> dict[str, object]:
    if sys.platform != "win32":
        return {
            "ok": False,
            "backend": "windows_sapi",
            "model": "installed_recognizer",
            "language": "",
            "fallback_from": "faster_whisper",
            "fallback_error": whisper_error,
            "error": "windows_sapi_requires_win32",
            "text_b64": "",
        }
    script = r"""
param([string]$AudioPath)
Add-Type -AssemblyName System.Speech
$recognizers = [System.Speech.Recognition.SpeechRecognitionEngine]::InstalledRecognizers()
$recognizerInfo = $recognizers | Where-Object { $_.Culture.Name -like 'zh*' } | Select-Object -First 1
if (-not $recognizerInfo) { $recognizerInfo = $recognizers | Select-Object -First 1 }
if (-not $recognizerInfo) { throw 'no installed Windows speech recognizer' }
$engine = [System.Speech.Recognition.SpeechRecognitionEngine]::new($recognizerInfo)
$grammar = [System.Speech.Recognition.DictationGrammar]::new()
$engine.LoadGrammar($grammar)
$engine.SetInputToWaveFile((Resolve-Path -LiteralPath $AudioPath).Path)
$result = $engine.Recognize([TimeSpan]::FromSeconds(20))
$engine.Dispose()
if ($null -eq $result) { '' } else { $result.Text }
"""
    try:
        completed = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                script,
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            encoding="utf-8",
            errors="replace",
        )
        text = completed.stdout.strip()
        if completed.returncode != 0:
            return {
                "ok": False,
                "backend": "windows_sapi",
                "model": "installed_recognizer",
                "language": "",
                "fallback_from": "faster_whisper",
                "fallback_error": whisper_error,
                "error": (completed.stderr or completed.stdout).strip(),
                "text_b64": "",
            }
        return {
            "ok": bool(text),
            "backend": "windows_sapi",
            "model": "installed_recognizer",
            "language": "",
            "fallback_from": "faster_whisper",
            "fallback_error": whisper_error,
            "error": "" if text else "windows_sapi_no_transcript",
            "text_b64": base64.b64encode(text.encode("utf-8")).decode("ascii"),
        }
    except Exception as exc:
        return {
            "ok": False,
            "backend": "windows_sapi",
            "model": "installed_recognizer",
            "language": "",
            "fallback_from": "faster_whisper",
            "fallback_error": whisper_error,
            "error": f"{type(exc).__name__}: {exc}",
            "text_b64": "",
        }


def _try_pocketsphinx(path: Path, model: str, *, whisper_error: str, sapi_error: str = "") -> dict[str, object]:
    try:
        import speech_recognition as sr

        recognizer = sr.Recognizer()
        with sr.AudioFile(str(path)) as source:
            audio = recognizer.record(source)
        text = recognizer.recognize_sphinx(audio)
        return {
            "ok": bool(text.strip()),
            "backend": "pocketsphinx",
            "model": "en-us",
            "language": "en",
            "fallback_from": "faster_whisper,windows_sapi",
            "fallback_error": whisper_error,
            "windows_sapi_error": sapi_error,
            "text_b64": base64.b64encode(text.encode("utf-8")).decode("ascii"),
        }
    except Exception as exc:
        return {
            "ok": False,
            "backend": "pocketsphinx",
            "model": "en-us",
            "language": "en",
            "fallback_from": "faster_whisper,windows_sapi",
            "fallback_error": whisper_error,
            "windows_sapi_error": sapi_error,
            "error": f"{type(exc).__name__}: {exc}",
            "text_b64": "",
        }


def _should_fallback_to_pocketsphinx(error: str) -> bool:
    if not error:
        return False
    markers = (
        "LocalEntryNotFoundError",
        "ConnectTimeout",
        "cannot find the appropriate snapshot",
        "model",
        "download",
        "Hub",
    )
    return any(marker.lower() in error.lower() for marker in markers)


if __name__ == "__main__":
    raise SystemExit(main())
