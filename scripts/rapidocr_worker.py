from __future__ import annotations

import argparse
import base64
import json
import os
import tempfile
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("image")
    args = parser.parse_args()
    image = Path(args.image)
    items = []
    backends = []
    variants, cleanup = _image_variants(image)
    try:
        rapid_items = _run_rapidocr(variants)
        if rapid_items:
            backends.append("rapidocr")
            items.extend(rapid_items)
        paddle_items = _run_paddleocr(variants)
        if paddle_items:
            backends.append("paddleocr")
            items.extend(paddle_items)
    finally:
        for item in cleanup:
            try:
                item.unlink()
            except OSError:
                pass
    items = _dedupe_items(items)
    text = "\n".join(item["text"] for item in items)
    payload = {
        "items": items,
        "backends": backends,
        "variant_count": len(variants),
        "text_b64": base64.b64encode(text.encode("utf-8")).decode("ascii"),
    }
    print("RAPIDOCR_JSON:" + json.dumps(payload, ensure_ascii=True))
    return 0


def _image_variants(image: Path) -> tuple[list[Path], list[Path]]:
    variants = [image]
    cleanup: list[Path] = []
    try:
        from PIL import Image, ImageEnhance, ImageFilter

        with Image.open(image) as img:
            rgb = img.convert("RGB")
            for scale in (2, 3):
                resized = rgb.resize((rgb.width * scale, rgb.height * scale), Image.Resampling.LANCZOS)
                enhanced = ImageEnhance.Contrast(resized).enhance(1.25).filter(ImageFilter.SHARPEN)
                fd, tmp_name = tempfile.mkstemp(suffix=f".ocr_{scale}x.png")
                os.close(fd)
                tmp = Path(tmp_name)
                try:
                    tmp.unlink()
                except OSError:
                    pass
                enhanced.save(tmp)
                variants.append(tmp)
                cleanup.append(tmp)
    except Exception:
        return variants, cleanup
    return variants, cleanup


def _run_rapidocr(images: list[Path]) -> list[dict[str, object]]:
    try:
        from rapidocr_onnxruntime import RapidOCR
    except Exception:
        return []
    engine = RapidOCR()
    items: list[dict[str, object]] = []
    for image in images:
        try:
            result, _ = engine(str(image))
        except Exception:
            continue
        if result:
            for row in result:
                box, text, score = row
                items.append({"text": str(text), "score": float(score), "box": box, "backend": "rapidocr"})
    return items


def _run_paddleocr(images: list[Path]) -> list[dict[str, object]]:
    os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")
    try:
        from paddleocr import PaddleOCR
    except Exception:
        return []
    engine = _paddle_engine(PaddleOCR)
    if engine is None:
        return []
    items: list[dict[str, object]] = []
    for image in images:
        try:
            result = _paddle_predict(engine, image)
        except Exception:
            continue
        items.extend(_paddle_items(result))
    return items


def _paddle_engine(factory):
    variants = [
        {
            "use_doc_orientation_classify": False,
            "use_doc_unwarping": False,
            "use_textline_orientation": True,
            "lang": "ch",
        },
        {"use_textline_orientation": True, "lang": "ch"},
        {"use_angle_cls": True, "lang": "ch"},
        {"lang": "ch"},
    ]
    for kwargs in variants:
        try:
            return factory(**kwargs)
        except Exception:
            continue
    return None


def _paddle_predict(engine, image: Path):
    predict = getattr(engine, "predict", None)
    if callable(predict):
        return predict(str(image))
    ocr = getattr(engine, "ocr", None)
    if callable(ocr):
        try:
            return ocr(str(image), cls=True)
        except TypeError:
            return ocr(str(image))
    return []


def _paddle_items(result) -> list[dict[str, object]]:
    items: list[dict[str, object]] = []
    _collect_paddle_items(result, items)
    return items


def _collect_paddle_items(value, items: list[dict[str, object]]) -> None:
    if value is None:
        return
    as_dict = _paddle_result_dict(value)
    if as_dict is not None:
        texts = as_dict.get("rec_texts") or as_dict.get("texts")
        scores = as_dict.get("rec_scores") or as_dict.get("scores") or []
        boxes = as_dict.get("rec_polys") or as_dict.get("dt_polys") or as_dict.get("boxes") or []
        if isinstance(texts, list):
            for index, text in enumerate(texts):
                clean = str(text).strip()
                if not clean:
                    continue
                score = _safe_float(scores[index] if index < len(scores) else 0.0)
                box = boxes[index] if index < len(boxes) else []
                items.append({"text": clean, "score": score, "box": box, "backend": "paddleocr"})
            return
    if _looks_like_old_paddle_row(value):
        try:
            text = str(value[1][0]).strip()
            if text:
                items.append({"text": text, "score": float(value[1][1]), "box": value[0], "backend": "paddleocr"})
        except Exception:
            pass
        return
    if isinstance(value, dict):
        for child in value.values():
            if isinstance(child, (list, tuple, dict)):
                _collect_paddle_items(child, items)
        return
    if isinstance(value, (list, tuple)):
        for child in value:
            _collect_paddle_items(child, items)


def _paddle_result_dict(value) -> dict[str, object] | None:
    if isinstance(value, dict):
        return value
    json_attr = getattr(value, "json", None)
    if isinstance(json_attr, dict):
        return json_attr
    if callable(json_attr):
        try:
            payload = json_attr()
            if isinstance(payload, dict):
                return payload
        except Exception:
            return None
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        try:
            payload = to_dict()
            if isinstance(payload, dict):
                return payload
        except Exception:
            return None
    return None


def _looks_like_old_paddle_row(value) -> bool:
    return (
        isinstance(value, (list, tuple))
        and len(value) >= 2
        and isinstance(value[1], (list, tuple))
        and len(value[1]) >= 2
    )


def _safe_float(value) -> float:
    try:
        return float(value)
    except Exception:
        return 0.0


def _dedupe_items(items: list[dict[str, object]]) -> list[dict[str, object]]:
    seen: set[tuple[str, int, int]] = set()
    deduped: list[dict[str, object]] = []
    for item in sorted(items, key=lambda value: -float(value.get("score", 0.0) or 0.0)):
        text = str(item.get("text", "")).strip()
        if not text:
            continue
        left, top = _box_origin(item.get("box", []))
        key = (text, int(left // 16), int(top // 16))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def _box_origin(box) -> tuple[float, float]:
    xs = []
    ys = []
    if isinstance(box, (list, tuple)):
        for point in box:
            if isinstance(point, (list, tuple)) and len(point) >= 2:
                try:
                    xs.append(float(point[0]))
                    ys.append(float(point[1]))
                except Exception:
                    pass
    return (min(xs) if xs else 0.0, min(ys) if ys else 0.0)


if __name__ == "__main__":
    raise SystemExit(main())
