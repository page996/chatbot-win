from __future__ import annotations

import argparse
import base64
import json
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("audio")
    parser.add_argument("--model", default="base")
    args = parser.parse_args()
    path = Path(args.audio)
    try:
        from faster_whisper import WhisperModel

        model = WhisperModel(args.model, device="auto", compute_type="auto")
        segments, info = model.transcribe(str(path), vad_filter=True)
        text = "\n".join(segment.text.strip() for segment in segments if segment.text.strip())
        payload = {
            "ok": bool(text.strip()),
            "backend": "faster_whisper",
            "model": args.model,
            "language": getattr(info, "language", "") or "",
            "text_b64": base64.b64encode(text.encode("utf-8")).decode("ascii"),
        }
    except Exception as exc:
        payload = {
            "ok": False,
            "backend": "faster_whisper",
            "model": args.model,
            "error": f"{type(exc).__name__}: {exc}",
            "text_b64": "",
        }
    print("LOCAL_ASR_JSON:" + json.dumps(payload, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
