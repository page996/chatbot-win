from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("audio")
    parser.add_argument("--model", default="base")
    parser.add_argument("--language", default="auto")
    args = parser.parse_args(argv)
    path = Path(args.audio)
    language = str(args.language or "auto").strip().lower()
    prepared_path, cleanup = _prepare_audio(path)
    try:
        funasr_payload = _try_funasr(prepared_path, args.model, language=language)
        if funasr_payload.get("ok") or not _should_fallback_from_funasr(str(funasr_payload.get("error", ""))):
            payload = funasr_payload
        else:
            whisper_payload = _try_faster_whisper(prepared_path, args.model, language=language)
            if whisper_payload.get("ok") or not _should_fallback_to_pocketsphinx(str(whisper_payload.get("error", ""))):
                payload = whisper_payload
            else:
                if language in {"en", "en-us", "english"}:
                    payload = _try_pocketsphinx(
                        prepared_path,
                        args.model,
                        whisper_error=str(whisper_payload.get("error", "")),
                        sapi_error="windows_sapi_not_used",
                    )
                elif _env_enabled("CHATBOT_WIN_ENABLE_WINDOWS_SAPI_ASR"):
                    payload = _try_windows_sapi(prepared_path, args.model, whisper_error=str(whisper_payload.get("error", "")))
                else:
                    payload = {
                        "ok": True,
                        "backend": "local_asr",
                        "model": args.model,
                        "language": "",
                        "fallback_from": "faster_whisper",
                        "fallback_error": str(whisper_payload.get("error", "")),
                        "error": "",
                        "text_b64": "",
                    }
    finally:
        for item in cleanup:
            try:
                item.unlink()
            except OSError:
                pass
    print("LOCAL_ASR_JSON:" + json.dumps(payload, ensure_ascii=True))
    return 0


def _prepare_audio(path: Path) -> tuple[Path, list[Path]]:
    ffmpeg = _find_ffmpeg()
    if ffmpeg is None:
        return path, []
    fd, tmp_name = tempfile.mkstemp(suffix=".16k.wav")
    os.close(fd)
    tmp = Path(tmp_name)
    command = [
        str(ffmpeg),
        "-y",
        "-i",
        str(path),
        "-ac",
        "1",
        "-ar",
        "16000",
        "-vn",
        str(tmp),
    ]
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=60, check=False)
    except Exception:
        return path, []
    if completed.returncode != 0 or not tmp.exists() or tmp.stat().st_size <= 44:
        try:
            tmp.unlink()
        except OSError:
            pass
        return path, []
    return tmp, [tmp]


def _find_ffmpeg() -> str | None:
    from shutil import which

    found = which("ffmpeg")
    if found:
        return found
    repo_root = Path(__file__).resolve().parents[1]
    candidates = [
        repo_root / "vendor" / "ffmpeg" / "bin" / "ffmpeg.exe",
        repo_root / "vendor" / "ffmpeg.exe",
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return None


def _try_funasr(path: Path, model: str, *, language: str) -> dict[str, object]:
    try:
        from funasr import AutoModel

        model_name = os.environ.get("CHATBOT_WIN_FUNASR_MODEL", "").strip() or "iic/SenseVoiceSmall"
        vad_model = os.environ.get("CHATBOT_WIN_FUNASR_VAD_MODEL", "").strip() or "fsmn-vad"
        asr = AutoModel(model=model_name, vad_model=vad_model, disable_update=True)
        language_hint = "auto" if language in {"", "auto"} else language
        result = asr.generate(input=str(path), language=language_hint)
        text = _funasr_text(result)
        return {
            "ok": True,
            "backend": "funasr",
            "model": model_name,
            "language": language_hint,
            "text_b64": base64.b64encode(text.encode("utf-8")).decode("ascii"),
            "error": "",
        }
    except ModuleNotFoundError as exc:
        return {
            "ok": False,
            "backend": "funasr",
            "model": "iic/SenseVoiceSmall",
            "error": f"missing_backend:{type(exc).__name__}: {exc}",
            "text_b64": "",
        }
    except Exception as exc:
        return {
            "ok": False,
            "backend": "funasr",
            "model": "iic/SenseVoiceSmall",
            "error": f"{type(exc).__name__}: {exc}",
            "text_b64": "",
        }


def _funasr_text(result: object) -> str:
    if isinstance(result, list):
        parts = []
        for item in result:
            if isinstance(item, dict):
                parts.append(str(item.get("text", "")).strip())
            elif item is not None:
                parts.append(str(item).strip())
        return "\n".join(part for part in parts if part)
    if isinstance(result, dict):
        return str(result.get("text", "")).strip()
    return str(result or "").strip()


def _try_faster_whisper(path: Path, model: str, *, language: str) -> dict[str, object]:
    try:
        from faster_whisper import WhisperModel

        whisper = WhisperModel(model, device="auto", compute_type="auto")
        language_hint = None if language in {"", "auto"} else language
        segments, info = whisper.transcribe(str(path), vad_filter=True, language=language_hint)
        text = "\n".join(segment.text.strip() for segment in segments if segment.text.strip())
        return {
            "ok": True,
            "backend": "faster_whisper",
            "model": model,
            "language": getattr(info, "language", "") or "",
            "error": "",
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


def _is_missing_backend(error: str) -> bool:
    return "missing_backend" in error or "No module named" in error


def _should_fallback_from_funasr(error: str) -> bool:
    if _is_missing_backend(error):
        return True
    return _should_fallback_to_pocketsphinx(error)


if __name__ == "__main__":
    raise SystemExit(main())
