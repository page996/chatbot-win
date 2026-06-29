from __future__ import annotations

import argparse
import base64
import json
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("image")
    args = parser.parse_args()
    from rapidocr_onnxruntime import RapidOCR

    engine = RapidOCR()
    result, _ = engine(str(Path(args.image)))
    items = []
    if result:
        for row in result:
            box, text, score = row
            items.append({"text": str(text), "score": float(score), "box": box})
    text = "\n".join(item["text"] for item in items)
    payload = {
        "items": items,
        "text_b64": base64.b64encode(text.encode("utf-8")).decode("ascii"),
    }
    print("RAPIDOCR_JSON:" + json.dumps(payload, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
