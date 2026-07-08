from __future__ import annotations

import argparse
import base64
import json
import os
import sysconfig
import tempfile
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("image")
    parser.add_argument("--prefer-gpu", action="store_true")
    parser.add_argument("--backend", choices=["auto", "gpu", "cpu", "rapidocr", "paddleocr"], default="auto")
    args = parser.parse_args()
    _ensure_nvidia_dll_paths()
    image = Path(args.image)
    items = []
    backends = []
    gpu_attempted = False
    variants, cleanup = _image_variants(image)
    profile = _image_profile(image)
    try:
        if args.backend in {"auto", "gpu", "paddleocr"} and (args.backend != "auto" or args.prefer_gpu):
            gpu_attempted = bool(args.prefer_gpu or args.backend == "gpu")
            paddle_items, paddle_backend, paddle_executed = _run_paddleocr(
                variants,
                prefer_gpu=bool(args.prefer_gpu or args.backend == "gpu"),
                require_gpu=args.backend == "gpu",
            )
            if paddle_items or paddle_executed:
                backends.append(paddle_backend)
                items.extend(paddle_items)
        if args.backend in {"auto", "cpu", "rapidocr"} and not items:
            rapid_items = _run_rapidocr(variants)
            if rapid_items:
                backends.append("rapidocr")
                items.extend(rapid_items)
        if args.backend in {"cpu", "paddleocr"} and not items:
            paddle_items, paddle_backend, paddle_executed = _run_paddleocr(
                variants,
                prefer_gpu=False,
                require_gpu=args.backend == "gpu",
            )
            if paddle_items or paddle_executed:
                backends.append(paddle_backend)
                items.extend(paddle_items)
    finally:
        for item in cleanup:
            try:
                item.unlink()
            except OSError:
                pass
    items = _dedupe_items(items)
    filter_reason = _sticker_false_positive_reason(items, profile)
    if filter_reason:
        items = []
    text = "\n".join(item["text"] for item in items)
    payload = {
        "items": items,
        "backends": backends,
        "variant_count": len(variants),
        "gpu_requested": bool(args.prefer_gpu or args.backend == "gpu"),
        "gpu_attempted": gpu_attempted,
        "gpu_used": any(str(backend).endswith("_gpu") for backend in backends),
        "image_profile": profile,
        "filter_reason": filter_reason,
        "text_b64": base64.b64encode(text.encode("utf-8")).decode("ascii"),
    }
    print("RAPIDOCR_JSON:" + json.dumps(payload, ensure_ascii=True))
    return 0


def _ensure_nvidia_dll_paths() -> None:
    """Expose NVIDIA pip-package DLLs to PaddleOCR on Windows.

    The cu13 Paddle wheel installs CUDA/cuDNN DLLs under site-packages/nvidia,
    but Windows does not automatically search those nested directories when
    Paddle creates the OCR model. Add them before importing paddleocr.
    """

    roots: list[Path] = []
    purelib = sysconfig.get_paths().get("purelib", "")
    if purelib:
        roots.append(Path(purelib) / "nvidia")
    repo_root = Path(__file__).resolve().parents[1]
    roots.append(repo_root / "vendor" / "ocr-python" / "Lib" / "site-packages" / "nvidia")
    rels = [
        Path("cu13") / "bin" / "x86_64",
        Path("cu13") / "bin",
        Path("cu13") / "lib",
        Path("cudnn") / "bin",
    ]
    paths: list[Path] = []
    for root in roots:
        for rel in rels:
            path = root / rel
            if path.is_dir() and path not in paths:
                paths.append(path)
    if not paths:
        return
    if os.name == "nt" and hasattr(os, "add_dll_directory"):
        for path in paths:
            try:
                os.add_dll_directory(str(path))
            except OSError:
                continue
    current_path = os.environ.get("PATH", "")
    prefix = os.pathsep.join(str(path) for path in paths)
    os.environ["PATH"] = prefix + (os.pathsep + current_path if current_path else "")


def _image_variants(image: Path) -> tuple[list[dict[str, object]], list[Path]]:
    variants: list[dict[str, object]] = [_variant(image, scale=1.0, offset_x=0.0, offset_y=0.0, name="original")]
    cleanup: list[Path] = []
    try:
        from PIL import Image, ImageEnhance, ImageFilter, ImageOps

        with Image.open(image) as img:
            try:
                img.seek(0)
            except Exception:
                pass
            img = ImageOps.exif_transpose(img)
            if img.mode in {"RGBA", "LA"} or (img.mode == "P" and "transparency" in img.info):
                background = Image.new("RGB", img.convert("RGBA").size, "white")
                background.paste(img.convert("RGBA"), mask=img.convert("RGBA").split()[-1])
                rgb = background
            else:
                rgb = img.convert("RGB")
            max_side = max(rgb.width, rgb.height)
            if max_side <= 1400:
                scales = (2.0, 3.0)
            elif max_side <= 3200:
                scales = (1.5, 2.0)
            else:
                scales = (1.25,)
            for scale in scales:
                variants.append(_save_enhanced_variant(rgb, cleanup, scale=scale, offset_x=0, offset_y=0, name=f"full_{scale:g}x"))
            variants.append(_save_grayscale_variant(rgb, cleanup, offset_x=0, offset_y=0, name="gray_autocontrast"))
            variants.append(_save_binary_variant(rgb, cleanup, offset_x=0, offset_y=0, name="binary_text"))
            if rgb.height > 2400:
                variants.extend(_tile_variants(rgb, cleanup, axis="vertical"))
            if rgb.width > 2400 and rgb.height <= 2400:
                variants.extend(_tile_variants(rgb, cleanup, axis="horizontal"))
    except Exception:
        return variants, cleanup
    return variants, cleanup


def _image_profile(image: Path) -> dict[str, object]:
    try:
        from PIL import Image, ImageOps

        with Image.open(image) as img:
            animated = bool(getattr(img, "is_animated", False))
            try:
                img.seek(0)
            except Exception:
                pass
            img = ImageOps.exif_transpose(img)
            width, height = img.size
            alpha_ratio = 0.0
            if img.mode in {"RGBA", "LA"} or (img.mode == "P" and "transparency" in img.info):
                alpha = img.convert("RGBA").split()[-1]
                histogram = alpha.histogram()
                transparent = sum(histogram[:16])
                alpha_ratio = transparent / max(1, width * height)
            aspect = width / max(1, height)
            sticker_candidate = (
                max(width, height) <= 640
                and 0.55 <= aspect <= 1.8
                and (animated or alpha_ratio >= 0.03 or min(width, height) <= 180)
            )
            return {
                "width": width,
                "height": height,
                "animated": animated,
                "alpha_ratio": round(alpha_ratio, 4),
                "sticker_candidate": sticker_candidate,
            }
    except Exception:
        return {"sticker_candidate": False}
    return {"sticker_candidate": False}


def _variant(path: Path, *, scale: float, offset_x: float, offset_y: float, name: str) -> dict[str, object]:
    return {"path": path, "scale": scale, "offset_x": offset_x, "offset_y": offset_y, "variant": name}


def _save_enhanced_variant(image, cleanup: list[Path], *, scale: float, offset_x: float, offset_y: float, name: str) -> dict[str, object]:
    from PIL import Image, ImageEnhance, ImageFilter

    width = max(1, int(image.width * scale))
    height = max(1, int(image.height * scale))
    resized = image.resize((width, height), Image.Resampling.LANCZOS)
    enhanced = ImageEnhance.Contrast(resized).enhance(1.35).filter(ImageFilter.SHARPEN)
    fd, tmp_name = tempfile.mkstemp(suffix=f".ocr_{name}.png")
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        tmp.unlink()
    except OSError:
        pass
    enhanced.save(tmp)
    cleanup.append(tmp)
    return _variant(tmp, scale=scale, offset_x=offset_x, offset_y=offset_y, name=name)


def _save_grayscale_variant(image, cleanup: list[Path], *, offset_x: float, offset_y: float, name: str) -> dict[str, object]:
    from PIL import ImageOps

    gray = ImageOps.autocontrast(image.convert("L")).convert("RGB")
    return _save_temp_variant(gray, cleanup, scale=1.0, offset_x=offset_x, offset_y=offset_y, name=name)


def _save_binary_variant(image, cleanup: list[Path], *, offset_x: float, offset_y: float, name: str) -> dict[str, object]:
    from PIL import ImageOps

    gray = ImageOps.autocontrast(image.convert("L"))
    binary = gray.point(lambda value: 255 if value > 175 else 0, mode="1").convert("RGB")
    return _save_temp_variant(binary, cleanup, scale=1.0, offset_x=offset_x, offset_y=offset_y, name=name)


def _save_temp_variant(image, cleanup: list[Path], *, scale: float, offset_x: float, offset_y: float, name: str) -> dict[str, object]:
    fd, tmp_name = tempfile.mkstemp(suffix=f".ocr_{name}.png")
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        tmp.unlink()
    except OSError:
        pass
    image.save(tmp)
    cleanup.append(tmp)
    return _variant(tmp, scale=scale, offset_x=offset_x, offset_y=offset_y, name=name)


def _tile_variants(image, cleanup: list[Path], *, axis: str) -> list[dict[str, object]]:
    variants: list[dict[str, object]] = []
    tile_size = 1800
    overlap = 180
    step = tile_size - overlap
    limit = 40
    if axis == "vertical":
        start = 0
        index = 0
        while start < image.height and index < limit:
            end = min(image.height, start + tile_size)
            crop = image.crop((0, start, image.width, end))
            scale = 1.5 if max(crop.width, crop.height) <= 2200 else 1.0
            variants.append(_save_enhanced_variant(crop, cleanup, scale=scale, offset_x=0, offset_y=start, name=f"tile_y_{index:03d}"))
            if end >= image.height:
                break
            start += step
            index += 1
    else:
        start = 0
        index = 0
        while start < image.width and index < limit:
            end = min(image.width, start + tile_size)
            crop = image.crop((start, 0, end, image.height))
            scale = 1.5 if max(crop.width, crop.height) <= 2200 else 1.0
            variants.append(_save_enhanced_variant(crop, cleanup, scale=scale, offset_x=start, offset_y=0, name=f"tile_x_{index:03d}"))
            if end >= image.width:
                break
            start += step
            index += 1
    return variants


def _run_rapidocr(images: list[dict[str, object]]) -> list[dict[str, object]]:
    try:
        from rapidocr_onnxruntime import RapidOCR
    except Exception:
        return []
    engine = RapidOCR()
    items: list[dict[str, object]] = []
    for variant in images:
        image = Path(str(variant["path"]))
        try:
            result, _ = engine(str(image))
        except Exception:
            continue
        if result:
            for row in result:
                box, text, score = row
                items.append(
                    {
                        "text": str(text),
                        "score": float(score),
                        "box": _normalize_box(box, variant),
                        "backend": "rapidocr",
                        "variant": variant.get("variant", ""),
                    }
                )
    return items


def _run_paddleocr(
    images: list[dict[str, object]],
    *,
    prefer_gpu: bool,
    require_gpu: bool,
) -> tuple[list[dict[str, object]], str, bool]:
    os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")
    try:
        from paddleocr import PaddleOCR
    except Exception:
        if require_gpu:
            raise
        return [], "paddleocr_gpu" if prefer_gpu else "paddleocr", False
    engine, backend = _paddle_engine(PaddleOCR, prefer_gpu=prefer_gpu, require_gpu=require_gpu)
    if engine is None:
        if require_gpu:
            raise RuntimeError("GPU OCR required but CUDA-enabled PaddleOCR engine could not be created")
        return [], backend, False
    items: list[dict[str, object]] = []
    executed = False
    for variant in images:
        image = Path(str(variant["path"]))
        try:
            executed = True
            result = _paddle_predict(engine, image)
        except Exception:
            continue
        items.extend(_paddle_items(result, variant, backend=backend))
    return items, backend, executed


def _paddle_engine(factory, *, prefer_gpu: bool, require_gpu: bool):
    gpu_ready = _paddle_gpu_ready()
    base_variants = [
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
    candidates: list[tuple[dict[str, object], str]] = []
    if prefer_gpu and gpu_ready:
        for kwargs in base_variants:
            candidates.append(({**kwargs, "device": "gpu:0"}, "paddleocr_gpu"))
            candidates.append(({**kwargs, "device": "gpu"}, "paddleocr_gpu"))
            candidates.append(({**kwargs, "use_gpu": True}, "paddleocr_gpu"))
    if not require_gpu:
        candidates.extend((kwargs, "paddleocr") for kwargs in base_variants)
    for kwargs, backend in candidates:
        try:
            return factory(**kwargs), backend
        except Exception:
            continue
    return None, "paddleocr_gpu" if prefer_gpu else "paddleocr"


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


def _paddle_items(result, variant: dict[str, object], *, backend: str) -> list[dict[str, object]]:
    items: list[dict[str, object]] = []
    _collect_paddle_items(result, items, variant, backend=backend)
    return items


def _collect_paddle_items(value, items: list[dict[str, object]], variant: dict[str, object], *, backend: str) -> None:
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
                items.append(
                    {
                        "text": clean,
                        "score": score,
                        "box": _normalize_box(box, variant),
                        "backend": backend,
                        "variant": variant.get("variant", ""),
                    }
                )
            return
    if _looks_like_old_paddle_row(value):
        try:
            text = str(value[1][0]).strip()
            if text:
                items.append(
                    {
                        "text": text,
                        "score": float(value[1][1]),
                        "box": _normalize_box(value[0], variant),
                        "backend": backend,
                        "variant": variant.get("variant", ""),
                    }
                )
        except Exception:
            pass
        return
    if isinstance(value, dict):
        for child in value.values():
            if isinstance(child, (list, tuple, dict)):
                _collect_paddle_items(child, items, variant, backend=backend)
        return
    if isinstance(value, (list, tuple)):
        for child in value:
            _collect_paddle_items(child, items, variant, backend=backend)


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
        and not isinstance(value[1][0], (list, tuple, dict))
    )


def _safe_float(value) -> float:
    try:
        return float(value)
    except Exception:
        return 0.0


def _paddle_gpu_ready() -> bool:
    try:
        import paddle

        return bool(getattr(paddle.device, "is_compiled_with_cuda", lambda: False)())
    except Exception:
        return False


def _normalize_box(box, variant: dict[str, object]) -> list[list[float]]:
    scale = _safe_float(variant.get("scale", 1.0)) or 1.0
    offset_x = _safe_float(variant.get("offset_x", 0.0))
    offset_y = _safe_float(variant.get("offset_y", 0.0))
    points: list[list[float]] = []
    if isinstance(box, (list, tuple)):
        for point in box:
            if isinstance(point, (list, tuple)) and len(point) >= 2:
                try:
                    points.append([float(point[0]) / scale + offset_x, float(point[1]) / scale + offset_y])
                except Exception:
                    continue
            elif hasattr(point, "tolist"):
                try:
                    raw = point.tolist()
                    if isinstance(raw, (list, tuple)) and len(raw) >= 2:
                        points.append([float(raw[0]) / scale + offset_x, float(raw[1]) / scale + offset_y])
                except Exception:
                    continue
    elif hasattr(box, "tolist"):
        return _normalize_box(box.tolist(), variant)
    return points


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


def _sticker_false_positive_reason(items: list[dict[str, object]], profile: dict[str, object]) -> str:
    if not bool(profile.get("sticker_candidate", False)):
        return ""
    texts = [str(item.get("text", "")).strip() for item in items if str(item.get("text", "")).strip()]
    if not texts:
        return ""
    compact = "".join(texts)
    normalized = compact.strip().lower()
    if len(normalized) > 2:
        return ""
    scores = []
    for item in items:
        try:
            scores.append(float(item.get("score", 0.0) or 0.0))
        except Exception:
            continue
    mean_score = sum(scores) / len(scores) if scores else 0.0
    if normalized in {"j", "l", "i", "|", "1"} and mean_score < 0.82:
        return "likely_sticker_single_char_false_positive"
    if len(normalized) == 1 and mean_score < 0.62:
        return "likely_sticker_low_confidence_single_char"
    return ""


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
