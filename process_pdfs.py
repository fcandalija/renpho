#!/usr/bin/env python3
"""RENPHO segmental-analysis pipeline.

The RENPHO "Report Details" PDFs are flat images (no text), so the numeric
values must be read from the pixels. This script handles the whole loop:

  ingest Pull freshly exported files out of the inbox folders (an iCloud Drive
         folder, and ~/Downloads as a fallback) into place: new report PDFs are
         moved into reports/ (de-duplicated by content hash, renamed to the next
         free number) and the newest RENPHO Health CSV replaces the main CSV
         (old one backed up). Then runs `auto`. A launchd agent runs this
         automatically whenever files land — see renpho-ingest.plist.

  auto   Find PDFs in reports/ not yet recorded, OCR their segmental numbers
         locally (ocr_segmental.py -> the `tesseract` binary), append the clean
         reads to segmental_data.jsonl, and run `build`. Any PDF whose read is
         incomplete or fails the consistency check is flagged for `scan`
         instead of being trusted. This is the one-command path.

  scan   Manual fallback: crop the date header and both segmental sections into
         _work/staging/ and print a JSON skeleton to fill in by eye (used when
         `auto` flags a report, or if tesseract is unavailable).

  build  Rebuild RENPHO Segmental-Francisco.csv from segmental_data.jsonl
         (matching each record to the main CSV by date + time), then merge it
         with the main CSV into RENPHO Combined-Francisco.csv and embed that
         combined data into dashboard.html. If the dashboard changed, it is
         committed and pushed to the public repo so GitHub Pages redeploys
         (`publish`). Only dashboard.html is ever pushed.

  publish Commit + push dashboard.html to origin on its own. Runs automatically
         at the end of `build`; useful by hand if an earlier push failed.
         Set RENPHO_PUBLISH=0 to keep builds local.

Typical loop when adding new reports:
  1. Drop the new PDF(s) into renpho/reports/
  2. python3 process_pdfs.py auto        # OCR + append + build, in one step

Dependencies: the Python stdlib, pdfimg.py, and (for `auto`) the `tesseract`
binary on PATH. `scan`/`build` need no third-party anything.
"""
import json, csv, os, sys, glob, re, shutil, hashlib, subprocess, time, fnmatch
from datetime import datetime
import pdfimg

HERE     = os.path.dirname(os.path.abspath(__file__))
REPORTS  = os.path.join(HERE, "reports")
DATA     = os.path.join(HERE, "segmental_data.jsonl")
MAIN_CSV = os.path.join(HERE, "RENPHO Health-Francisco.csv")
OUT_CSV  = os.path.join(HERE, "RENPHO Segmental-Francisco.csv")
COMBINED_CSV = os.path.join(HERE, "RENPHO Combined-Francisco.csv")
DASHBOARD    = os.path.join(HERE, "dashboard.html")
STAGING  = os.path.join(HERE, "_work", "staging")
BACKUPS  = os.path.join(HERE, "_work", "backups")


# --- log timestamps ----------------------------------------------------------
# Every command reports with plain print(), and launchd points stdout+stderr at
# _work/ingest.log. Rather than stamp ~40 call sites, wrap the streams and
# prefix each line as it's written — which also catches tracebacks.
class _TimestampedStream:
    """Prefixes every non-blank output line with the local time.

    Tracks line state across writes because print() emits the text and its "\\n"
    as separate write() calls. Blank lines pass through unstamped so the spacing
    the commands use as separators stays readable."""

    def __init__(self, stream):
        self._s = stream
        self._at_line_start = True

    def write(self, text):
        if not text:
            return 0
        out = []
        for part in text.splitlines(keepends=True):
            if self._at_line_start and part.strip():
                out.append(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] ")
            out.append(part)
            self._at_line_start = part.endswith("\n")
        self._s.write("".join(out))
        if self._at_line_start:
            # Flush on each completed line: the log is block-buffered when
            # redirected, and stdout/stderr share the file, so without this the
            # two streams land out of order and a long run logs nothing until it
            # finishes.
            self._s.flush()
        return len(text)

    def __getattr__(self, name):
        return getattr(self._s, name)


def _install_log_timestamps():
    """Stamp output when it's redirected (i.e. the launchd log), leaving
    interactive terminal runs clean. Force either way with RENPHO_LOG_TIMESTAMPS."""
    env = os.environ.get("RENPHO_LOG_TIMESTAMPS")
    if env is not None:
        on = env.strip().lower() not in ("", "0", "false", "no", "off")
    else:
        try:
            on = not sys.stdout.isatty()
        except Exception:
            on = True
    if on:
        sys.stdout = _TimestampedStream(sys.stdout)
        sys.stderr = _TimestampedStream(sys.stderr)

# Where freshly exported files land. The iCloud inbox is the primary drop point
# (save RENPHO exports there from the phone; they sync to this Mac); ~/Downloads
# is kept as a fallback so AirDrop still works. Override with $RENPHO_INBOX.
ICLOUD_INBOX = os.path.expanduser(
    "~/Library/Mobile Documents/com~apple~CloudDocs/RENPHO-inbox")
DOWNLOADS    = os.path.expanduser("~/Downloads")

# Crop boxes are stable because every report uses the same fixed-layout template
# rendered at 3720x5262 px. (x0, y0, x1, y1)
HDR_BOX  = (1950, 455, 3700, 575)     # "Fecha de la prueba: ..." strip
BOTH_BOX = (72, 3130, 2130, 4280)     # both segmental sections, legibly

SEGMENTS = [("al", "Brazo izquierdo"), ("ar", "Brazo derecho"), ("t", "Torso"),
            ("ll", "Pierna izquierda"), ("lr", "Pierna derecha")]


def load_records():
    recs = []
    if os.path.exists(DATA):
        with open(DATA) as f:
            for line in f:
                line = line.strip()
                if line:
                    recs.append(json.loads(line))
    return recs


def safe_name(pdf_name):
    return pdf_name.replace(" ", "_").replace(".pdf", "")


def cmd_scan():
    recs = load_records()
    processed = {r["src"] for r in recs}
    pdfs = sorted(glob.glob(os.path.join(REPORTS, "*.pdf")))
    new = [p for p in pdfs if os.path.basename(p) not in processed]

    print(f"{len(pdfs)} PDF(s) in reports/, {len(processed)} already recorded, {len(new)} new.")
    if not new:
        print("Nothing to do. Run `build` if you edited the data file.")
        return

    os.makedirs(STAGING, exist_ok=True)
    print(f"\nGenerating crops into {STAGING} ...")
    skeletons = []
    for p in new:
        name = os.path.basename(p)
        base = safe_name(name)
        w, h, rgb, mask = pdfimg.load_image(p)
        hdr = os.path.join(STAGING, f"{base}__date.png")
        both = os.path.join(STAGING, f"{base}__sections.png")
        pdfimg.write_png(hdr, w, h, rgb, mask, box=HDR_BOX)
        pdfimg.write_png(both, w, h, rgb, mask, box=BOTH_BOX)
        print(f"  {name}")
        print(f"      date crop:     {hdr}")
        print(f"      sections crop: {both}")
        # skeleton: fill date/time from the date crop, and each [mass, %, std].
        skeletons.append({
            "src": name, "date": "M/D/YY", "time": "H:MM:SS AM",
            "fat": {k: [None, None, None] for k, _ in SEGMENTS},
            "mus": {k: [None, None, None] for k, _ in SEGMENTS},
        })

    tmpl = os.path.join(STAGING, "to_fill.jsonl")
    with open(tmpl, "w") as f:
        for s in skeletons:
            f.write(json.dumps(s, ensure_ascii=False) + "\n")
    print(f"\nSkeleton written to {tmpl}")
    print("Segment keys: al=Brazo izquierdo, ar=Brazo derecho, t=Torso, "
          "ll=Pierna izquierda, lr=Pierna derecha")
    print("Each value is [mass_lb, percent_vs_standard, standard_lb] "
          "(orange/blue diamond = mass, %; the third bullet = standard).")
    print(f"Fill the values by reading the crops, then append the lines to {DATA} and run `build`.")


def norm_time(s):
    return " ".join(s.strip().split())


def record_warnings(rec):
    """Consistency check for a single record: flag any triple whose shown %
    disagrees with mass/std*100 (a likely OCR/transcription slip). Small masses
    round hard, so only larger segments are checked, with a loose bound."""
    out = []
    for grp in ("fat", "mus"):
        for key, label in SEGMENTS:
            m, pct, std = rec[grp][key]
            if None in (m, pct, std) or not std:
                continue
            calc = m / std * 100
            if abs(calc - pct) > 15 and m >= 5:
                out.append(f"{grp} {label}: %={pct} but mass/std*100={calc:.1f}")
    return out


def _sources():
    """Folders to pull new exports from, most-preferred first."""
    srcs = []
    env = os.environ.get("RENPHO_INBOX")
    if env:
        srcs.append(os.path.expanduser(env))
    srcs += [ICLOUD_INBOX, DOWNLOADS]
    # de-dup while preserving order, keep only existing dirs
    seen, out = set(), []
    for s in srcs:
        if s not in seen and os.path.isdir(s):
            seen.add(s); out.append(s)
    return out


def _sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _existing_pdf_hashes():
    return {_sha(p): os.path.basename(p) for p in glob.glob(os.path.join(REPORTS, "*.pdf"))}


def _next_report_number():
    """Next free N for 'Report Details N.pdf' (the unnumbered file counts as 1)."""
    nums = [0]
    for p in glob.glob(os.path.join(REPORTS, "Report Details*.pdf")):
        m = re.match(r"Report Details(?: (\d+))?\.pdf$", os.path.basename(p))
        if m:
            nums.append(int(m.group(1)) if m.group(1) else 1)
    return max(nums) + 1


def _looks_like_health_csv(path):
    try:
        with open(path, newline="") as f:
            header = next(csv.reader(f))
        return all(c in header for c in ("No.", "Fecha", "Hora"))
    except Exception:
        return False


def _backup(path):
    if os.path.exists(path):
        os.makedirs(BACKUPS, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        shutil.copy2(path, os.path.join(BACKUPS, f"{os.path.basename(path)}.{stamp}.bak"))


def _row_dt(d):
    """Parse a row dict's Fecha+Hora into a datetime for sorting; unparseable -> min."""
    try:
        return datetime.strptime(f"{d.get('Fecha', '').strip()} {norm_time(d.get('Hora', ''))}",
                                 "%m/%d/%y %I:%M:%S %p")
    except Exception:
        return datetime.min


def _merge_health_csv(incoming_path):
    """Merge an incoming Health CSV into MAIN_CSV instead of replacing it, so a
    partial export (only new days) adds/refreshes rows rather than wiping history.
    Rows are keyed by (Fecha, Hora); incoming rows win on conflict. The result is
    re-sorted newest-first and `No.` is renumbered. Returns (ok, added, updated)."""
    ih, irows = _read_csv(incoming_path)
    if "Fecha" not in ih or "Hora" not in ih:
        return False, 0, 0
    if os.path.exists(MAIN_CSV):
        mh, mrows = _read_csv(MAIN_CSV)
    else:
        mh, mrows = ih[:], []

    # Canonical columns: existing main header, plus any new columns the export added.
    cols = list(mh) + [c for c in ih if c not in mh]

    by_key = {}
    for r in mrows:
        d = dict(zip(mh, r))
        by_key[(d.get("Fecha", "").strip(), norm_time(d.get("Hora", "")))] = d
    added = updated = 0
    for r in irows:
        d = dict(zip(ih, r))
        k = (d.get("Fecha", "").strip(), norm_time(d.get("Hora", "")))
        if k in by_key:
            by_key[k].update(d)      # refresh fields from the newer export
            updated += 1
        else:
            by_key[k] = d
            added += 1

    rows = sorted(by_key.values(), key=_row_dt, reverse=True)   # newest first
    for i, d in enumerate(rows, 1):
        d["No."] = str(i)

    with open(MAIN_CSV, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(cols)
        w.writerows([[d.get(c, "") for c in cols] for d in rows])
    return True, added, updated


# --- iCloud placeholders -----------------------------------------------------
# A file in iCloud Drive that hasn't been downloaded to this Mac yet is a
# *placeholder*, and it shows up in one of two forms:
#
#   1. dataless  — the real name and size, but no bytes on disk. Marked with the
#                  SF_DATALESS flag (0x40000000). Opening it triggers a download
#                  and blocks until it lands, which is exactly what we want.
#   2. .icloud   — the older/evicted form: the file is *renamed* to a hidden stub
#                  ".<name>.icloud" holding only metadata. Note the real name is
#                  gone, so a plain glob for "Report Details*.pdf" never sees it.
#
# Both are fixed the same way: ask iCloud to materialize the file, then wait for
# it. `brctl download` kicks off the fetch; the blocking read is what actually
# guarantees the bytes are here before we try to parse the PDF.
SF_DATALESS = 0x40000000
DOWNLOAD_TIMEOUT = 90       # seconds to wait per file before giving up (retry next poll)
DOWNLOAD_POLL    = 0.5


def _is_dataless(path):
    """True if `path` is an undownloaded iCloud placeholder (form 1)."""
    try:
        return bool(os.stat(path, follow_symlinks=False).st_flags & SF_DATALESS)
    except (OSError, AttributeError):
        return False


def _icloud_stub_target(stub):
    """For a '.<name>.icloud' stub, the path the real file will have once it
    downloads; None if `stub` isn't one of those."""
    d, b = os.path.split(stub)
    if b.startswith(".") and b.endswith(".icloud"):
        return os.path.join(d, b[1:-len(".icloud")])
    return None


def _placeholders(folder, pattern):
    """Placeholder files in `folder` whose *real* name matches `pattern`, as
    (path_to_poke, expected_real_path) pairs. Covers both placeholder forms."""
    out = []
    try:
        names = os.listdir(folder)
    except OSError:
        return out
    for name in names:
        p = os.path.join(folder, name)
        real = _icloud_stub_target(p)
        if real is not None:                       # form 2: hidden .icloud stub
            if fnmatch.fnmatch(os.path.basename(real), pattern):
                out.append((p, real))
        elif fnmatch.fnmatch(name, pattern) and _is_dataless(p):   # form 1
            out.append((p, p))
    return out


def _force_download(poke_path, real_path, timeout=None):
    """Materialize one placeholder and wait for the bytes to actually arrive.

    Two nudges, because neither alone is reliable: `brctl download` asks the
    iCloud daemon to fetch it (and is the only thing that works on a .icloud
    stub, whose real name doesn't exist yet), and opening the file is what makes
    the *kernel* fault the data in for a dataless file. Returns True if
    `real_path` is present and non-placeholder before `timeout`.

    timeout=None reads DOWNLOAD_TIMEOUT at call time (not as a default argument,
    which would freeze the value at import and ignore any later override)."""
    if timeout is None:
        timeout = DOWNLOAD_TIMEOUT
    subprocess.run(["brctl", "download", poke_path], capture_output=True)

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if os.path.exists(real_path) and not _is_dataless(real_path):
            return True
        if os.path.exists(real_path):
            # Dataless: read a byte. On APFS this blocks until the fetch
            # completes (or errors), so it doubles as the wait.
            try:
                with open(real_path, "rb") as f:
                    f.read(1)
                if not _is_dataless(real_path):
                    return True
            except OSError:
                pass                # still materializing — fall through and retry
        time.sleep(DOWNLOAD_POLL)
    return os.path.exists(real_path) and not _is_dataless(real_path)


def _materialize_inbox(folder, patterns):
    """Force-download every placeholder in `folder` matching any of `patterns`.
    Returns (n_forced, n_failed)."""
    forced = failed = 0
    seen = set()
    for pattern in patterns:
        for poke, real in _placeholders(folder, pattern):
            if real in seen:
                continue
            seen.add(real)
            label = os.path.basename(real)
            print(f"  ↓ {label}: placeholder in iCloud — downloading …")
            if _force_download(poke, real):
                forced += 1
                print(f"  ↓ {label}: downloaded")
            else:
                failed += 1
                print(f"  ↓ {label}: still not available after "
                      f"{DOWNLOAD_TIMEOUT}s — will retry next poll")
    return forced, failed


def cmd_ingest():
    """Pull freshly exported files out of the inbox folders and into place, then
    run `auto`. Idempotent: PDFs are de-duplicated by content hash and renamed to
    the next free number (so a re-dropped or same-named file never clobbers or
    double-counts), and only complete, readable files are committed."""
    sources = _sources()
    # iCloud may hold these files as undownloaded placeholders. Materialize any
    # we care about *and wait for them* before the parse gate below, so a
    # not-yet-downloaded file gets ingested on this run instead of being
    # deferred (or, for a hidden .icloud stub, missed by the glob entirely).
    dl_forced = dl_failed = 0
    for s in sources:
        f, x = _materialize_inbox(s, ("Report Details*.pdf", "RENPHO Health*.csv"))
        dl_forced += f
        dl_failed += x

    existing = _existing_pdf_hashes()
    moved, dups, not_ready = [], 0, 0
    n = _next_report_number()
    for s in sources:
        for src in sorted(glob.glob(os.path.join(s, "Report Details*.pdf"))):
            try:
                pdfimg.load_image(src)          # gate: complete & parseable?
            except Exception:
                not_ready += 1                   # partial download / placeholder — retry later
                continue
            h = _sha(src)
            if h in existing:
                os.remove(src)                   # already recorded; clear it from the inbox
                dups += 1
                continue
            dest = os.path.join(REPORTS, f"Report Details {n}.pdf")
            while os.path.exists(dest):
                n += 1
                dest = os.path.join(REPORTS, f"Report Details {n}.pdf")
            shutil.move(src, dest)
            existing[h] = os.path.basename(dest)
            moved.append(os.path.basename(dest))
            n += 1

    # Main CSV: merge every valid Health export found (oldest first, so the
    # newest export wins on any overlapping day) into MAIN_CSV, keeping history.
    candidates = []
    for s in sources:
        candidates += [c for c in glob.glob(os.path.join(s, "RENPHO Health*.csv"))
                       if _looks_like_health_csv(c)]
    csv_updated, csv_added, csv_updated_rows = False, 0, 0
    if candidates:
        _backup(MAIN_CSV)
        for c in sorted(candidates, key=os.path.getmtime):
            ok, added, updated = _merge_health_csv(c)
            if ok:
                csv_updated = True
                csv_added += added
                csv_updated_rows += updated
                try:
                    os.remove(c)                 # consumed — clear from the inbox
                except OSError:
                    pass
            else:
                print(f"  ⚠ CSV {os.path.basename(c)} not merged (unexpected columns); left in inbox")

    did_something = bool(moved) or csv_updated
    # Stay silent on a pure no-op so the timer-driven polling doesn't spam the
    # log; only speak up when something happened or needs attention.
    if did_something or dups or not_ready or dl_forced or dl_failed:
        csv_note = (f"CSV +{csv_added}/~{csv_updated_rows} rows merged" if csv_updated
                    else "CSV unchanged")
        dl_note = ""
        if dl_forced or dl_failed:
            dl_note = f", {dl_forced} downloaded from iCloud"
            if dl_failed:
                dl_note += f" ({dl_failed} timed out)"
        # No explicit timestamp here — _install_log_timestamps() stamps every line.
        print(f"ingest: {len(moved)} new PDF(s), "
              f"{dups} duplicate(s) cleared, {not_ready} not-ready, {csv_note}{dl_note}")
        for m in moved:
            print(f"  + reports/{m}")
        if not_ready:
            print(f"  ({not_ready} file(s) not fully synced yet — will be picked up next poll)")

    if did_something:
        print()
        cmd_auto(build=False)   # OCR + append any new PDF records (no build yet)
        cmd_build()             # single rebuild from the merged CSV + updated data


def cmd_auto(build=True):
    """Detect new PDFs, OCR their segmental numbers locally, append the clean
    ones to the data file, and rebuild. PDFs whose read is incomplete or fails
    the consistency check are flagged for manual review (`scan`) instead.
    With build=False, skip the rebuild (the caller rebuilds once itself)."""
    import ocr_segmental
    recs = load_records()
    processed = {r["src"] for r in recs}
    pdfs = sorted(glob.glob(os.path.join(REPORTS, "*.pdf")))
    new = [p for p in pdfs if os.path.basename(p) not in processed]

    print(f"{len(pdfs)} PDF(s) in reports/, {len(processed)} already recorded, {len(new)} new.")
    if not new:
        if build:
            print("Nothing to do. Run `build` if you edited the data file.")
        return

    ok, flagged = [], []
    for p in new:
        name = os.path.basename(p)
        try:
            rec, warnings = ocr_segmental.extract_full(p)
        except Exception as e:
            flagged.append(name)
            print(f"  ✗ {name}: OCR failed: {e}")
            continue
        warnings = list(warnings) + record_warnings(rec)
        if warnings:
            flagged.append(name)
            print(f"  ⚠ {name}: needs review ({rec['date']} {rec['time']})")
            for w in warnings:
                print(f"        {w}")
        else:
            ok.append(rec)
            print(f"  ✓ {name}  ({rec['date']} {rec['time']})")

    if ok:
        with open(DATA, "a") as f:
            for rec in ok:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        print(f"\nAppended {len(ok)} record(s) to {DATA}")
    if flagged:
        print(f"\n{len(flagged)} PDF(s) NOT recorded automatically. Read the crops and "
              f"fill them in manually:\n  python3 process_pdfs.py scan")
    if ok:
        print()
        if build:
            cmd_build()


def cmd_build():
    recs = load_records()
    by_dt = {(r["date"], norm_time(r["time"])): r for r in recs}

    with open(MAIN_CSV, newline="") as f:
        rows = [row for row in csv.reader(f) if any(c.strip() for c in row)]
    header = rows[0]
    i_no, i_fecha, i_hora = header.index("No."), header.index("Fecha"), header.index("Hora")

    def cols_for(prefix):
        out = []
        for _, label in SEGMENTS:
            out += [f"{prefix} {label}(lb)", f"{prefix} {label}(%)", f"{prefix} {label} estándar(lb)"]
        return out
    out_header = ["No.", "Fecha", "Hora"] + cols_for("Grasa") + cols_for("Músculo")

    def fmt(v):
        return "" if v is None else f"{float(v):.1f}"

    out_rows, matched, unmatched, warnings = [], 0, [], []
    for row in rows[1:]:
        date, time = row[i_fecha].strip(), norm_time(row[i_hora])
        rec = by_dt.get((date, time))
        line = [row[i_no].strip(), date, row[i_hora].strip()]
        if rec:
            matched += 1
            for grp in ("fat", "mus"):
                for key, label in SEGMENTS:
                    m, pct, std = rec[grp][key]
                    line += [fmt(m), fmt(pct), fmt(std)]
                    # gross-error check (small masses round hard, so use a loose bound)
                    if None not in (m, pct, std) and std:
                        calc = m / std * 100
                        if abs(calc - pct) > 15 and m >= 5:
                            warnings.append(f"{date} {grp} {label}: %={pct} but mass/std*100={calc:.1f}")
        else:
            unmatched.append((row[i_no].strip(), date, time))
            line += [""] * (len(out_header) - 3)
        out_rows.append(line)

    with open(OUT_CSV, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(out_header)
        w.writerows(out_rows)

    print(f"Wrote {OUT_CSV}")
    print(f"main rows: {len(rows)-1}, matched: {matched}, blank: {len(unmatched)}")
    for u in unmatched:
        print(f"  no PDF data yet for No.{u[0]} ({u[1]} {u[2]})")
    used = {(r[i_fecha].strip(), norm_time(r[i_hora])) for r in rows[1:]}
    for r in recs:
        if (r["date"], norm_time(r["time"])) not in used:
            print(f"  WARNING: record {r['src']} ({r['date']} {r['time']}) matches no main-CSV row")
    for wmsg in warnings:
        print(f"  CHECK: {wmsg}")

    # Merge main + segmental into the combined CSV and refresh the dashboard.
    combine_and_embed()


def _read_csv(path):
    with open(path, newline="") as f:
        rows = [r for r in csv.reader(f) if any(c.strip() for c in r)]
    return rows[0], rows[1:]


def combine_and_embed():
    """Join main + segmental on Fecha/Hora into COMBINED_CSV, then embed it in the dashboard."""
    mh, mrows = _read_csv(MAIN_CSV)
    sh, srows = _read_csv(OUT_CSV)
    im_f, im_h = mh.index("Fecha"), mh.index("Hora")
    is_f, is_h = sh.index("Fecha"), sh.index("Hora")

    seg_cols = [c for c in sh if c not in ("No.", "Fecha", "Hora")]
    seg_idx = [sh.index(c) for c in seg_cols]
    seg_by_dt = {(r[is_f].strip(), norm_time(r[is_h])): r for r in srows}

    out_header = mh + seg_cols
    out_rows = []
    for r in mrows:
        s = seg_by_dt.get((r[im_f].strip(), norm_time(r[im_h])))
        out_rows.append(r + ([s[i] for i in seg_idx] if s else [""] * len(seg_cols)))

    with open(COMBINED_CSV, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(out_header)
        w.writerows(out_rows)
    print(f"Wrote {COMBINED_CSV} ({len(out_rows)} rows, {len(out_header)} cols)")

    # Pull first, so the dashboard we embed into is the newest published code
    # rather than whatever this machine last built.
    _sync_from_origin()

    # Embed the combined CSV into the dashboard's <script id="embedded-csv"> block.
    html = _embed_into(open(DASHBOARD, encoding="utf-8").read(), _combined_text())
    if html is None:
        print("  WARNING: could not find embedded-csv block in dashboard.html; skipped embed")
        return
    open(DASHBOARD, "w", encoding="utf-8").write(html)
    print(f"Embedded combined data into {DASHBOARD}")

    _publish_dashboard()


# The dashboard is code + one generated data block. Everything outside this block
# is hand-written and must survive a data refresh, wherever the refresh runs.
EMBED_RE = re.compile(r'(<script id="embedded-csv" type="text/plain">)(.*?)(</script>)', re.S)


def _combined_text():
    """The combined CSV exactly as it goes into the dashboard."""
    return open(COMBINED_CSV, encoding="utf-8").read().rstrip("\n")


def _embed_into(html, combined_text):
    """`html` with its data block replaced. None if there's no block to replace —
    callers must treat that as "don't touch this file", not as an empty result."""
    if not EMBED_RE.search(html):
        return None
    return EMBED_RE.sub(lambda m: m.group(1) + combined_text + m.group(3), html, count=1)


# --- publishing to GitHub Pages ----------------------------------------------
# dashboard.html is committed and pushed to the public repo (origin) so GitHub
# Pages serves the latest data. Two hard rules, because this repo also holds raw
# health data that is NOT published:
#
#   1. Only dashboard.html is ever staged — by explicit path, never `git add -A`.
#      The CSVs, reports/ PDFs, segmental_data.jsonl and _work/ logs all contain
#      personal measurements and must stay local. `.gitignore` is a second layer.
#   2. A publish failure never fails the ingest run. This is called at the tail of
#      a pipeline that has already written good data locally; a network or auth
#      problem is logged and the next update retries.
PUBLISH_FILE = "dashboard.html"        # relative to HERE; the only published path
GIT_TIMEOUT  = 120                     # per-git-command cap, so a hung network op
                                       # can't outlast the 2-minute poll interval

# Identity for the published commits. Pinned here rather than left to git config
# because this script also runs on other machines, whose global ~/.gitconfig is a
# work address — and this repo is public. Overridable with $RENPHO_GIT_EMAIL.
PUBLISH_EMAIL = os.environ.get("RENPHO_GIT_EMAIL",
                               "fcandalija@users.noreply.github.com")


def _publish_enabled():
    """On by default; RENPHO_PUBLISH=0 turns pushing off (local-only builds)."""
    v = os.environ.get("RENPHO_PUBLISH")
    if v is None:
        return True
    return v.strip().lower() not in ("", "0", "false", "no", "off")


def _git_env():
    """Environment for non-interactive git. Without this a missing credential or
    unknown host key makes git sit waiting on a prompt that no one can answer —
    which, under launchd, is a hang rather than an error."""
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"        # never ask for username/password
    env.setdefault("GIT_SSH_COMMAND",
                   "ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new "
                   "-o ConnectTimeout=15")  # never ask for a passphrase
    return env


def _git(*args, check=True):
    """Run a git command in HERE (not the caller's cwd — launchd's differs)."""
    return subprocess.run(("git",) + args, cwd=HERE, env=_git_env(),
                          capture_output=True, text=True,
                          timeout=GIT_TIMEOUT, check=check)


def _git_out(*args):
    """Stdout of a git command, or None if it fails."""
    try:
        r = _git(*args, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def _git_commit(msg):
    """Commit ONLY the dashboard, with the publish identity forced on. `-c` beats
    any global user.email, so a work address can't end up in the public log."""
    return _git("-c", f"user.email={PUBLISH_EMAIL}", "commit", "-m", msg,
                "--only", "--", PUBLISH_FILE)


def _dashboard_summary():
    """Row/column count for the commit message, best-effort."""
    try:
        h, rows = _read_csv(COMBINED_CSV)
        newest = rows[0][h.index("Fecha")].strip() if rows else "?"
        return f"{len(rows)} rows, latest {newest}"
    except Exception:
        return "data update"


def _sync_from_origin():
    """Fast-forward to origin/<branch> before the data is embedded, so a code
    change pushed from another machine (or made on github.com) becomes the base
    for this build. Without this, every such change had to be recovered at push
    time by `_push`'s reconcile — which works, but only after a failed push, and
    only if the remote's dashboard still has an embedded-csv block.

    Deliberately conservative, because a wrong move here loses hand-written code:

      * fast-forward only. If this machine has local commits (a diverged branch)
        or uncommitted edits to dashboard.html, git refuses and we leave the tree
        exactly as it was — the build continues on the local copy and `_push`
        reconciles afterwards, same as before.
      * never `reset --hard`, never `-A`. Only the fetch touches the network and
        only tracked code moves; the ignored CSVs and reports/ are untouchable.
      * never fails the ingest run. A network or auth problem is logged; the data
        still lands locally and the next update retries."""
    if not _publish_enabled():
        return
    if _git_out("rev-parse", "--is-inside-work-tree") != "true":
        return                              # not a clone — nothing to pull from
    if not _git_out("remote"):
        return
    branch = _git_out("rev-parse", "--abbrev-ref", "HEAD") or "main"
    try:
        _git("fetch", "origin", branch)
        head, upstream = _git_out("rev-parse", "HEAD"), _git_out("rev-parse", "FETCH_HEAD")
        if not upstream or head == upstream:
            return                          # already at the remote tip
        # Behind (fetched tip contains our HEAD)? Then a fast-forward is safe.
        # Ahead or diverged? Leave it to the push-time reconcile.
        if _git("merge-base", "--is-ancestor", "HEAD", upstream,
                check=False).returncode != 0:
            return
        r = _git("merge", "--ff-only", upstream, check=False)
        if r.returncode == 0:
            print(f"  publish: fast-forwarded to origin/{branch} before embedding")
        else:
            # Usually "local changes would be overwritten" — an uncommitted edit
            # on this machine. Don't force it; nothing is lost by building local.
            print("  publish: could not fast-forward, building on the local "
                  f"dashboard. {_tail(r.stderr or r.stdout)}")
    except subprocess.TimeoutExpired:
        print(f"  publish: fetch timed out after {GIT_TIMEOUT}s; building on the local dashboard")
    except (OSError, subprocess.SubprocessError) as e:
        print(f"  publish: pre-build pull skipped ({e}); building on the local dashboard")


def _publish_dashboard():
    """Commit dashboard.html and push it to origin so Pages picks it up.

    No-ops (silently) when the file is byte-identical to what's already
    committed, so the 2-minute poll doesn't pile up empty commits."""
    if not _publish_enabled():
        return
    if _git_out("rev-parse", "--is-inside-work-tree") != "true":
        return                              # not a clone — nothing to publish to
    if not _git_out("remote"):
        print("  publish: no git remote configured; skipped")
        return

    # Anything to publish? Compare the file against HEAD, not the index, so a
    # stray `git add` elsewhere doesn't make us think there's a change.
    try:
        unchanged = _git("diff", "--quiet", "HEAD", "--", PUBLISH_FILE,
                         check=False).returncode == 0
    except (OSError, subprocess.SubprocessError) as e:
        print(f"  publish: could not diff {PUBLISH_FILE} ({e}); skipped")
        return
    if unchanged:
        return                              # dashboard identical to what's live

    branch = _git_out("rev-parse", "--abbrev-ref", "HEAD") or "main"
    msg = f"[other] dashboard: update embedded data ({_dashboard_summary()})"
    try:
        # Stage ONLY the dashboard. Never `-A`: the health data must not leak.
        # "[other]" prefix is a repo convention (a hook rejects untagged messages);
        # this is a generated data refresh, not hand-written code.
        _git("add", "--", PUBLISH_FILE)
        _git_commit(msg)
        print(f"  publish: committed {PUBLISH_FILE}")
    except subprocess.CalledProcessError as e:
        print(f"  publish: commit failed; left staged. {_tail(e.stderr or e.stdout)}")
        return
    except (OSError, subprocess.SubprocessError) as e:
        print(f"  publish: commit failed ({e})")
        return

    if _push(branch, msg):
        print(f"  publish: pushed to {branch} — Pages will redeploy shortly")


def _push(branch, msg):
    """Push to origin, reconciling once if the remote moved ahead (e.g. an edit
    made on github.com, or a code change pushed from another machine). Returns
    True on success; logs and returns False otherwise — the commit stays local
    and the next update retries.

    `_sync_from_origin` fast-forwards before the build, so this path is now the
    fallback for the cases it can't handle: a remote change that landed during
    this run, or a local tree it wouldn't force.

    Reconciling deliberately does NOT rebase. dashboard.html holds a generated
    data block, so if both sides changed it a rebase conflicts every time and the
    update can never land. Instead we hard-reset onto the remote tip and re-embed
    our data into *the remote's* dashboard, then commit just that file. The split
    is: code comes from the remote, data comes from us. That keeps hand-written
    changes made elsewhere (a new chart, a UI fix) instead of reverting them to
    whatever this machine last built — while the data still always wins from here,
    since this is the host that owns the CSVs.

    The hard reset never touches the health data: the CSVs, reports/ and
    segmental_data.jsonl are all ignored, and the data we re-embed is read back
    from COMBINED_CSV. It *can* discard uncommitted changes to tracked code
    though — the scripts are published too now, not just dashboard.html — so it
    is guarded below rather than run unconditionally."""
    try:
        r = _git("push", "origin", f"HEAD:{branch}", check=False)
        if r.returncode == 0:
            return True

        print("  publish: remote moved ahead; re-embedding data on top of it and retrying")
        _git("fetch", "origin", branch)

        # Adopting the remote's tree wholesale is the point of this path, but it
        # must not eat someone's uncommitted work. dashboard.html is the one file
        # we're entitled to overwrite (we rebuild it from the CSVs anyway); a local
        # edit to anything else means a human was mid-change here, so defer the
        # push instead. Normally there's nothing to find: this runs after the
        # commit, on a machine that only ever generates data. `diff --name-only`
        # rather than `status --porcelain` because it emits bare paths — no status
        # column for _git_out's strip() to shift out from under the parse.
        stray = [p for p in (_git_out("diff", "--name-only", "HEAD") or "").split("\n")
                 if p.strip() and p.strip() != PUBLISH_FILE]
        if stray:
            print("  publish: uncommitted changes to tracked code "
                  f"({', '.join(p.strip() for p in stray[:3])}); push deferred "
                  "rather than reset over them — commit or stash, next run retries")
            return False

        _git("reset", "--hard", "-q", f"origin/{branch}")

        # The remote's dashboard.html is on disk now. Keep it and swap only the
        # data block; overwriting it with our copy would silently revert any code
        # change this machine hasn't pulled yet.
        merged = _embed_into(open(DASHBOARD, encoding="utf-8").read(), _combined_text())
        if merged is None:
            print("  publish: remote dashboard.html has no embedded-csv block; push "
                  "deferred rather than overwrite it — needs a look by hand")
            return False
        open(DASHBOARD, "w", encoding="utf-8").write(merged)

        _git("add", "--", PUBLISH_FILE)
        if _git("diff", "--cached", "--quiet", check=False).returncode == 0:
            print("  publish: remote already has this dashboard; nothing to push")
            return False
        _git_commit(msg)

        r = _git("push", "origin", f"HEAD:{branch}", check=False)
        if r.returncode == 0:
            return True
        print(f"  publish: push failed. {_tail(r.stderr or r.stdout)}")
    except subprocess.TimeoutExpired:
        print(f"  publish: git timed out after {GIT_TIMEOUT}s; will retry next update")
    except subprocess.CalledProcessError as e:
        print(f"  publish: reconcile failed, push deferred. {_tail(e.stderr or e.stdout)}")
    except (OSError, subprocess.SubprocessError) as e:
        print(f"  publish: push failed ({e})")
    return False


def _tail(text, n=2):
    """Last n non-blank lines of git output, for a one-line log message."""
    lines = [l.strip() for l in (text or "").splitlines() if l.strip()]
    return " | ".join(lines[-n:]) if lines else "(no output)"


if __name__ == "__main__":
    _install_log_timestamps()
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "ingest":
        cmd_ingest()
    elif cmd == "auto":
        cmd_auto()
    elif cmd == "scan":
        cmd_scan()
    elif cmd == "build":
        cmd_build()
    elif cmd == "publish":
        _publish_dashboard()
    else:
        print(__doc__)
        print("Usage: python3 process_pdfs.py [ingest|auto|scan|build|publish]")
        sys.exit(1)
