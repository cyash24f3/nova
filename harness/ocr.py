"""Local OCR for PDF pages without text; native pages retain their original text."""
from __future__ import annotations

import hashlib
from functools import lru_cache
from importlib.metadata import version
from pathlib import Path

import pymupdf

from harness import common

READER_VERSION = "pdf-ocr-v2"


@lru_cache(maxsize=1)
def _engine():
    # The pinned package includes its ONNX models. No cloud request is made.
    from rapidocr_onnxruntime import RapidOCR

    return RapidOCR(intra_op_num_threads=2, inter_op_num_threads=1,
                    det_limit_side_len=1536, max_side_len=3000)


def arrange_words(results) -> tuple[str, list[dict]]:
    """Group neighbouring boxes by baseline, then read each row left to right."""
    words = []
    for box, text, score, *_ in results or []:
        if not str(text).strip():
            continue
        xs, ys = zip(*box)
        words.append({"text": str(text).strip(), "score": round(float(score), 6),
                      "box": [[round(float(x), 2), round(float(y), 2)] for x, y in box],
                      "x": min(xs), "y": (min(ys) + max(ys)) / 2,
                      "height": max(ys) - min(ys)})
    rows = []
    for word in sorted(words, key=lambda word: (word["y"], word["x"])):
        if rows and abs(word["y"] - rows[-1][0]["y"]) <= max(3, min(word["height"], rows[-1][0]["height"]) * .45):
            rows[-1].append(word)
        else:
            rows.append([word])
    ordered = [sorted(row, key=lambda word: word["x"]) for row in rows]
    text = "\n".join(" ".join(word["text"] for word in row) for row in ordered)
    tokens = [{key: word[key] for key in ("text", "score", "box")}
              for row in ordered for word in row]
    return text, tokens


def _validate_cache(value, digest, count):
    if (value.get("reader") != READER_VERSION or value.get("sha256") != digest
            or len(value.get("pages", [])) != count
            or len(value.get("page_sources", [])) != count):
        raise ValueError("OCR cache does not match its source PDF.")
    for text, source in zip(value["pages"], value["page_sources"]):
        if not isinstance(text, str) or source.get("method") not in ("native", "ocr"):
            raise ValueError("Invalid OCR cache page.")
        for token in source.get("tokens", []):
            if not 0 <= token["score"] <= 1:
                raise ValueError("Invalid OCR recognition score.")


def read_pdf(raw: bytes, pages: list[str], state_dir: str) -> dict:
    """Return extra provenance only when OCR is needed, preserving old cache keys."""
    if all(page.strip() for page in pages):
        return {"pages": pages}
    digest = hashlib.sha256(raw).hexdigest()
    key = hashlib.sha256(f"{READER_VERSION}:{version('rapidocr-onnxruntime')}:{digest}".encode()).hexdigest()
    path = Path(state_dir) / "ocr" / f"{key}.json"
    if path.is_file():
        import json

        value = json.loads(path.read_text(encoding="utf-8"))
        _validate_cache(value, digest, len(pages))
    else:
        value = {"reader": READER_VERSION, "sha256": digest, "pages": list(pages), "page_sources": []}
        with pymupdf.open(stream=raw, filetype="pdf") as pdf:
            for index, text in enumerate(pages):
                if text.strip():
                    source = {"method": "native"}
                else:
                    pixmap = pdf[index].get_pixmap(matrix=pymupdf.Matrix(3.5, 3.5),
                                                 colorspace=pymupdf.csRGB, alpha=False)
                    try:
                        results, _ = _engine()(pixmap.tobytes("png"))
                    except Exception as exc:
                        raise RuntimeError(f"Local PDF OCR failed ({type(exc).__name__}).") from exc
                    text, tokens = arrange_words(results)
                    value["pages"][index] = text
                    source = {"method": "ocr", "tokens": tokens,
                              "mean_score": round(sum(t["score"] for t in tokens) / len(tokens), 6) if tokens else 0,
                              "min_score": min((t["score"] for t in tokens), default=0)}
                value["page_sources"].append(source)
        _validate_cache(value, digest, len(pages))
        common.write_json(path, value)
    return {"pages": value["pages"], "page_sources": value["page_sources"]}
