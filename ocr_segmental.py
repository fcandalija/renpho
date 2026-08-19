#!/usr/bin/env python3
"""OCR the segmental numbers from a RENPHO 'Report Details' PDF.

The reports are flat images, but every one uses the same pixel-stable template
(3720x5262). This reads the values *without* a vision model: it crops the two
segmental sections and the date header, runs the local `tesseract` binary in
TSV mode (word text + bounding box per token), and assigns each number to its
segment/value slot by position.

Layout of the "sections" crop (BOTH_BOX, 2058 px wide):
  - x < half  -> "Análisis segmental de grasa" (fat);  x >= half -> "Balance muscular" (mus)
  - three row bands by y: arms (top), torso (middle), legs (bottom)
  - within a segment the three numbers stack vertically -> [mass_lb, percent, standard_lb]

Each number is a float with exactly one decimal (e.g. 0.8, 47.3, 110.0), which
makes it trivial to separate real values from OCR noise ("lb", "%", "Estándar").

Dependencies: the Python stdlib + pdfimg.py + the `tesseract` binary on PATH.
"""
import json, os, re, subprocess, sys, tempfile
import pdfimg

# Crop boxes match process_pdfs.py (same fixed template).
HDR_BOX  = (1950, 455, 3700, 575)
BOTH_BOX = (72, 3130, 2130, 4280)

# Distinguishing label word -> segment key. Arms use masculine (izquierdo/derecho),
# legs feminine (izquierda/derecha), so every key is unambiguous.
LABEL_KEY = {
    "izquierdo": "al", "derecho": "ar", "torso": "t",
    "izquierda": "ll", "derecha": "lr",
}
SEG_KEYS = ["al", "ar", "t", "ll", "lr"]

NUM_RE   = re.compile(r"(?<!\d)\d{1,3}\.\d(?!\d)")   # a value: exactly one decimal digit
MONTHS   = {m: i + 1 for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
     "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"])}


class OCRError(Exception):
    pass


def _tsv(png_path, psm=6):
    """Run tesseract in TSV mode; return [{text,cx,cy,conf}, ...] for real words."""
    out = subprocess.run(
        ["tesseract", png_path, "stdout", "--psm", str(psm), "tsv"],
        capture_output=True, text=True, check=True).stdout
    tokens = []
    for line in out.splitlines()[1:]:
        f = line.split("\t")
        if len(f) < 12:
            continue
        left, top, width, height, conf, text = f[6], f[7], f[8], f[9], f[10], f[11]
        text = text.strip()
        if not text:
            continue
        tokens.append({
            "text": text,
            "cx": int(left) + int(width) / 2,
            "cy": int(top) + int(height) / 2,
            "conf": float(conf),
        })
    return tokens


def _write_crop(pdf_path, box):
    w, h, rgb, mask = pdfimg.load_image(pdf_path)
    fd, path = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    pdfimg.write_png(path, w, h, rgb, mask, box=box)
    return path


def _parse_header(pdf_path):
    png = _write_crop(pdf_path, HDR_BOX)
    try:
        text = subprocess.run(
            ["tesseract", png, "stdout", "--psm", "7"],
            capture_output=True, text=True, check=True).stdout
    finally:
        os.remove(png)
    m = re.search(r"prueba:\s*([A-Za-z]{3})\s+(\d{1,2}),\s*(\d{4})\s+at\s+"
                  r"(\d{1,2}:\d{2}:\d{2})\s*([AP]M)", text)
    if not m:
        raise OCRError(f"could not parse date header: {text.strip()!r}")
    mon, day, year, clock, ampm = m.groups()
    if mon not in MONTHS:
        raise OCRError(f"unknown month {mon!r} in header: {text.strip()!r}")
    date = f"{MONTHS[mon]}/{int(day)}/{year[2:]}"
    return date, f"{clock} {ampm}"


def _nearest_label(tok, labels):
    """Assign a numeric token to the closest label above it in the same section."""
    best, best_d = None, None
    for lab in labels:
        if lab["cy"] >= tok["cy"]:          # label must be above the number
            continue
        d = (tok["cx"] - lab["cx"]) ** 2 + (tok["cy"] - lab["cy"]) ** 2
        if best_d is None or d < best_d:
            best, best_d = lab, d
    return best


def extract(pdf_path):
    """Return (record_dict, warnings). record_dict has fat/mus -> key -> [m, pct, std]."""
    png = _write_crop(pdf_path, BOTH_BOX)
    try:
        tokens = _tsv(png)
    finally:
        os.remove(png)

    # Section split at the crop's horizontal midpoint (fat = left, muscle = right).
    split_x = (BOTH_BOX[2] - BOTH_BOX[0]) / 2

    labels = {"fat": [], "mus": []}
    for t in tokens:
        key = LABEL_KEY.get(t["text"].lower())
        if key:
            sect = "fat" if t["cx"] < split_x else "mus"
            labels[sect].append({"key": key, "cx": t["cx"], "cy": t["cy"]})

    groups = {"fat": {k: [] for k in SEG_KEYS}, "mus": {k: [] for k in SEG_KEYS}}
    for t in tokens:
        # OCR sometimes glues the bullet glyph and unit onto the number
        # (e.g. "°44.6%"), so search for the value inside the token.
        m = NUM_RE.search(t["text"].replace(",", "."))
        if not m:
            continue
        sect = "fat" if t["cx"] < split_x else "mus"
        lab = _nearest_label(t, labels[sect])
        if lab is None:
            continue
        groups[sect][lab["key"]].append((t["cy"], float(m.group())))

    warnings = []
    record = {"fat": {}, "mus": {}}
    for sect in ("fat", "mus"):
        for k in SEG_KEYS:
            vals = [v for _, v in sorted(groups[sect][k])]  # top->bottom = mass,%,std
            if len(vals) != 3:
                warnings.append(f"{sect}.{k}: expected 3 numbers, read {len(vals)} ({vals})")
                vals = (vals + [None, None, None])[:3]
            record[sect][k] = vals
    return record, warnings


def extract_full(pdf_path):
    """Full record including src/date/time, matching segmental_data.jsonl shape."""
    date, time = _parse_header(pdf_path)
    rec, warnings = extract(pdf_path)
    return {"src": os.path.basename(pdf_path), "date": date, "time": time,
            "fat": rec["fat"], "mus": rec["mus"]}, warnings


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python3 ocr_segmental.py <report.pdf>")
        sys.exit(1)
    rec, warnings = extract_full(sys.argv[1])
    print(json.dumps(rec, ensure_ascii=False))
    for w in warnings:
        print(f"  WARN: {w}", file=sys.stderr)
