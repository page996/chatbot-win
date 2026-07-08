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
    parser.add_argument("--device-mode", choices=["auto", "gpu", "cpu"], default="auto")
    args = parser.parse_args(argv)
    path = Path(args.audio)
    language = str(args.language or "auto").strip().lower()
    device_mode = _normalize_device_mode(args.device_mode)
    prepared_path, cleanup = _prepare_audio(path)
    try:
        cuda_available = _cuda_available() if device_mode == "gpu" else False
        if device_mode == "gpu" and not cuda_available:
            payload = _failed_payload(args.model, "gpu_required_but_cuda_unavailable", backend="local_asr_gpu")
        elif device_mode == "gpu" and cuda_available:
            payload = _transcribe_with_backends(prepared_path, args.model, language=language, device="cuda", strict_gpu=device_mode == "gpu")
        else:
            payload = _transcribe_with_backends(prepared_path, args.model, language=language, device="cpu", strict_gpu=False)
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


def _transcribe_with_backends(
    path: Path,
    model: str,
    *,
    language: str,
    device: str,
    strict_gpu: bool,
    fallback_from: str = "",
    fallback_error: str = "",
) -> dict[str, object]:
    if device != "cuda":
        return _transcribe_cpu_light_first(
            path,
            model,
            language=language,
            fallback_from=fallback_from,
            fallback_error=fallback_error,
        )

    funasr_payload = _try_funasr(path, model, language=language, device=device)
    if funasr_payload.get("ok"):
        return _with_fallback_note(funasr_payload, fallback_from=fallback_from, fallback_error=fallback_error)
    if strict_gpu or not _should_fallback_from_funasr(str(funasr_payload.get("error", ""))):
        if device == "cuda":
            whisper_payload = _try_faster_whisper(path, model, language=language, device=device)
            if whisper_payload.get("ok") or strict_gpu or not _should_fallback_to_pocketsphinx(str(whisper_payload.get("error", ""))):
                return _with_fallback_note(whisper_payload, fallback_from="funasr", fallback_error=str(funasr_payload.get("error", "")))
        return _with_fallback_note(funasr_payload, fallback_from=fallback_from, fallback_error=fallback_error)
    whisper_payload = _try_faster_whisper(path, model, language=language, device=device)
    if whisper_payload.get("ok") or strict_gpu or not _should_fallback_to_pocketsphinx(str(whisper_payload.get("error", ""))):
        return _with_fallback_note(whisper_payload, fallback_from="funasr", fallback_error=str(funasr_payload.get("error", "")))
    if language in {"en", "en-us", "english"}:
        return _try_pocketsphinx(
            path,
            model,
            whisper_error=str(whisper_payload.get("error", "")),
            sapi_error="windows_sapi_not_used",
        )
    if _env_enabled("CHATBOT_WIN_ENABLE_WINDOWS_SAPI_ASR"):
        return _try_windows_sapi(path, model, whisper_error=str(whisper_payload.get("error", "")))
    return {
        "ok": True,
        "backend": "local_asr",
        "model": model,
        "language": "",
        "fallback_from": "faster_whisper",
        "fallback_error": str(whisper_payload.get("error", "")),
        "error": "",
        "text_b64": "",
    }


def _transcribe_cpu_light_first(
    path: Path,
    model: str,
    *,
    language: str,
    fallback_from: str = "",
    fallback_error: str = "",
) -> dict[str, object]:
    """Default CPU path: use the light dependency before legacy heavy stacks."""

    whisper_payload = _try_faster_whisper(path, model, language=language, device="cpu")
    if whisper_payload.get("ok") or not _should_fallback_from_whisper_to_funasr(str(whisper_payload.get("error", ""))):
        return _with_fallback_note(whisper_payload, fallback_from=fallback_from, fallback_error=fallback_error)

    whisper_error = str(whisper_payload.get("error", ""))
    funasr_payload = _try_funasr(path, model, language=language, device="cpu")
    if funasr_payload.get("ok") or not _should_fallback_from_funasr(str(funasr_payload.get("error", ""))):
        return _with_fallback_note(
            funasr_payload,
            fallback_from=_join_fallback_sources(fallback_from, "faster_whisper"),
            fallback_error=_join_fallback_errors(fallback_error, whisper_error),
        )

    funasr_error = str(funasr_payload.get("error", ""))
    combined_error = _join_fallback_errors(whisper_error, funasr_error)
    if language in {"en", "en-us", "english"}:
        return _try_pocketsphinx(
            path,
            model,
            whisper_error=combined_error,
            sapi_error="windows_sapi_not_used",
        )
    if _env_enabled("CHATBOT_WIN_ENABLE_WINDOWS_SAPI_ASR"):
        return _try_windows_sapi(path, model, whisper_error=combined_error)
    return {
        "ok": True,
        "backend": "local_asr",
        "model": model,
        "language": "",
        "fallback_from": _join_fallback_sources(fallback_from, "faster_whisper,funasr"),
        "fallback_error": combined_error,
        "error": "",
        "text_b64": "",
    }


def _try_funasr(path: Path, model: str, *, language: str, device: str) -> dict[str, object]:
    if device == "cuda" and not _torch_cuda_available():
        return {
            "ok": False,
            "backend": "funasr_gpu",
            "model": "iic/SenseVoiceSmall",
            "error": "torch_cuda_unavailable_for_funasr",
            "text_b64": "",
            "device": device,
        }
    try:
        from funasr import AutoModel

        model_name = os.environ.get("CHATBOT_WIN_FUNASR_MODEL", "").strip() or "iic/SenseVoiceSmall"
        vad_model = os.environ.get("CHATBOT_WIN_FUNASR_VAD_MODEL", "").strip() or "fsmn-vad"
        kwargs = {"model": model_name, "vad_model": vad_model, "disable_update": True}
        if device == "cuda":
            kwargs["device"] = "cuda:0"
        else:
            kwargs["device"] = "cpu"
        try:
            asr = AutoModel(**kwargs)
        except TypeError:
            kwargs.pop("device", None)
            asr = AutoModel(**kwargs)
        language_hint = "auto" if language in {"", "auto"} else language
        result = asr.generate(input=str(path), language=language_hint)
        text = _funasr_text(result)
        return {
            "ok": True,
            "backend": "funasr_gpu" if device == "cuda" else "funasr_cpu",
            "model": model_name,
            "language": language_hint,
            "text_b64": base64.b64encode(text.encode("utf-8")).decode("ascii"),
            "error": "",
            "device": device,
        }
    except ModuleNotFoundError as exc:
        return {
            "ok": False,
            "backend": "funasr_gpu" if device == "cuda" else "funasr_cpu",
            "model": "iic/SenseVoiceSmall",
            "error": f"missing_backend:{type(exc).__name__}: {exc}",
            "text_b64": "",
            "device": device,
        }
    except Exception as exc:
        return {
            "ok": False,
            "backend": "funasr_gpu" if device == "cuda" else "funasr_cpu",
            "model": "iic/SenseVoiceSmall",
            "error": f"{type(exc).__name__}: {exc}",
            "text_b64": "",
            "device": device,
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


def _try_faster_whisper(path: Path, model: str, *, language: str, device: str) -> dict[str, object]:
    try:
        from faster_whisper import WhisperModel

        errors: list[str] = []
        for candidate_model in _whisper_model_candidates(model):
            local_ready = _local_whisper_model_ready(candidate_model)
            if local_ready is False and not _env_enabled("CHATBOT_WIN_ASR_ALLOW_MODEL_DOWNLOAD"):
                errors.append(f"{candidate_model}: local faster-whisper cache is incomplete; skipped")
                continue
            try:
                compute_type = "float16" if device == "cuda" else "int8"
                try:
                    whisper = WhisperModel(candidate_model, device=device, compute_type=compute_type)
                except Exception:
                    whisper = WhisperModel(candidate_model, device=device, compute_type="auto")
                language_hint = None if language in {"", "auto"} else language
                segments, info = whisper.transcribe(str(path), vad_filter=True, language=language_hint)
                text = "\n".join(segment.text.strip() for segment in segments if segment.text.strip())
                payload = {
                    "ok": True,
                    "backend": "faster_whisper_gpu" if device == "cuda" else "faster_whisper_cpu",
                    "model": candidate_model,
                    "language": getattr(info, "language", "") or "",
                    "error": "",
                    "text_b64": base64.b64encode(text.encode("utf-8")).decode("ascii"),
                    "device": device,
                }
                if candidate_model != model:
                    payload["fallback_from_model"] = model
                return payload
            except Exception as exc:
                errors.append(f"{candidate_model}: {type(exc).__name__}: {exc}")
        error = " | ".join(errors)
        return {
            "ok": False,
            "backend": "faster_whisper_gpu" if device == "cuda" else "faster_whisper_cpu",
            "model": model,
            "error": error,
            "text_b64": "",
            "device": device,
        }
    except Exception as exc:
        return {
            "ok": False,
            "backend": "faster_whisper_gpu" if device == "cuda" else "faster_whisper_cpu",
            "model": model,
            "error": f"{type(exc).__name__}: {exc}",
            "text_b64": "",
            "device": device,
        }


def _whisper_model_candidates(model: str) -> list[str]:
    candidates: list[str] = []
    for item in [model, *os.environ.get("CHATBOT_WIN_ASR_FALLBACK_MODELS", "tiny").split(",")]:
        cleaned = str(item or "").strip()
        if cleaned and cleaned not in candidates:
            candidates.append(cleaned)
    return candidates or ["tiny"]


def _local_whisper_model_ready(model: str) -> bool | None:
    cache_dir = _hf_cache_dir_for_whisper_model(model)
    if cache_dir is None or not cache_dir.exists():
        return None
    snapshots = cache_dir / "snapshots"
    if any(snapshot.is_dir() and (snapshot / "model.bin").is_file() for snapshot in snapshots.glob("*")):
        return True
    if any(cache_dir.rglob("*.incomplete")) or any(snapshots.glob("*")):
        return False
    return None


def _hf_cache_dir_for_whisper_model(model: str) -> Path | None:
    cleaned = str(model or "").strip().replace("\\", "/")
    if not cleaned:
        return None
    path = Path(cleaned)
    if path.is_absolute() or "/" in cleaned and not cleaned.startswith("Systran/"):
        return path if path.exists() else None
    repo = cleaned if "/" in cleaned else f"Systran/faster-whisper-{cleaned}"
    hf_home = Path(os.environ.get("HF_HOME", "") or (Path.home() / ".cache" / "huggingface"))
    return hf_home / "hub" / ("models--" + repo.replace("/", "--"))


def _failed_payload(model: str, error: str, *, backend: str) -> dict[str, object]:
    return {"ok": False, "backend": backend, "model": model, "language": "", "error": error, "text_b64": ""}


def _with_fallback_note(payload: dict[str, object], *, fallback_from: str, fallback_error: str) -> dict[str, object]:
    if fallback_from:
        payload = dict(payload)
        payload.setdefault("fallback_from", fallback_from)
        payload.setdefault("fallback_error", fallback_error)
    return payload


def _normalize_device_mode(mode: str) -> str:
    cleaned = str(mode or "auto").strip().lower()
    if cleaned in {"gpu", "cuda", "gpu-only", "gpu_only"}:
        return "gpu"
    if cleaned in {"cpu", "cpu-only", "cpu_only"}:
        return "cpu"
    return "auto"


def _cuda_available() -> bool:
    if _torch_cuda_available():
        return True
    try:
        import ctranslate2

        return int(getattr(ctranslate2, "get_cuda_device_count", lambda: 0)()) > 0
    except Exception:
        return False


def _env_enabled(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _torch_cuda_available() -> bool:
    try:
        import torch

        return bool(torch.cuda.is_available())
    except Exception:
        return False


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
    if "torch_cuda_unavailable_for_funasr" in error:
        return True
    return _should_fallback_to_pocketsphinx(error)


def _should_fallback_from_whisper_to_funasr(error: str) -> bool:
    if _is_missing_backend(error):
        return True
    return _should_fallback_to_pocketsphinx(error)


def _join_fallback_sources(*values: str) -> str:
    parts: list[str] = []
    for value in values:
        for item in str(value or "").split(","):
            cleaned = item.strip()
            if cleaned and cleaned not in parts:
                parts.append(cleaned)
    return ",".join(parts)


def _join_fallback_errors(*values: str) -> str:
    parts = [str(value).strip() for value in values if str(value or "").strip()]
    return " | ".join(parts)


if __name__ == "__main__":
    raise SystemExit(main())
