# RENPHO metrics — dashboard & segmental pipeline

## Setup: run the ingest periodically on a Mac
This is how the pipeline runs today: a **launchd user agent** (`com.francisco.renpho-ingest`) fires **every 2 minutes** (`StartInterval` 120), plus at login and whenever the iCloud inbox changes (best effort). Each run starts `RENPHO Ingest.app`, which runs `python3 process_pdfs.py ingest`. That run pulls new files from the iCloud inbox or `~/Downloads`, OCRs the reports, rebuilds the CSVs and `dashboard.html`, and pushes the dashboard to GitHub. The sections further down explain *why* each piece is needed.

### 1. Prerequisites
```sh
xcode-select --install          # clang, lipo, codesign (for building the launcher) + git
brew install tesseract python   # OCR; Homebrew python3 is preferred, /usr/bin/python3 (3.7+) also works
```
- **An SSH key that can push to this repo without a prompt.** Use a key with no passphrase, or a deploy key with *Allow write access*. The agent runs headless and will never answer a passphrase prompt.
- **iCloud Drive** turned on, with a folder named **`RENPHO-inbox`** at its root (`~/Library/Mobile Documents/com~apple~CloudDocs/RENPHO-inbox`). Override it with `$RENPHO_INBOX` if needed.

### 2. Clone and bring over the data
```sh
git clone git@github.com:fcandalija/renpho.git ~/repos/renpho
cd ~/repos/renpho
mkdir -p _work reports          # _work/ holds the log; launchd won't create the directory
```
Only the code and `dashboard.html` are in git. The personal data is deliberately not (see `.gitignore`), so copy it from the machine that already runs the agent:
`RENPHO Health-Francisco.csv`, `RENPHO Segmental-Francisco.csv`, `RENPHO Combined-Francisco.csv`, `segmental_data.jsonl` and the `reports/` folder.

Sanity check that it works by hand before scheduling it:
```sh
RENPHO_PUBLISH=0 python3 process_pdfs.py build
```

### 3. Build the launcher app
```sh
sh launcher/build.sh            # produces "RENPHO Ingest.app" (universal, ad-hoc signed)
```

### 4. Grant it Full Disk Access
System Settings → Privacy & Security → **Full Disk Access** → **+** → ⇧⌘G → `~/repos/renpho/` → select **`RENPHO Ingest.app`** → toggle it **on**.
Without this step the agent sees an *empty* iCloud inbox and `~/Downloads`, and macOS shows no prompt or error. **Repeat this step after every rebuild**: remove the old entry, then add the app again.

### 5. Install the launchd agent
`renpho-ingest.plist` has absolute paths for the original machine. This command writes a copy with your paths filled in:
```sh
REPO="$PWD"   # run from the repo root
sed -e "s#/Users/fcandalija/repos/renpho#$REPO#g" \
    -e "s#/Users/fcandalija#$HOME#g" \
    renpho-ingest.plist > ~/Library/LaunchAgents/com.francisco.renpho-ingest.plist
plutil -lint ~/Library/LaunchAgents/com.francisco.renpho-ingest.plist
```
Then load it. Run this **in Terminal.app, inside your own login session**. It fails over ssh or from other contexts:
```sh
launchctl bootout gui/$(id -u)/com.francisco.renpho-ingest 2>/dev/null
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.francisco.renpho-ingest.plist
```
Run the same two lines again whenever you edit the plist. To change how often it runs, edit `StartInterval` (in seconds).

### 6. Verify / operate
```sh
launchctl list | grep renpho-ingest                       # PID  last-exit-status  label
launchctl kickstart -k gui/$(id -u)/com.francisco.renpho-ingest   # run now
tail -f _work/ingest.log                                  # timestamped log, one line per event
launchctl bootout gui/$(id -u)/com.francisco.renpho-ingest        # disable
```
Common failures:
- **Nothing is ever picked up.** Full Disk Access is missing or stale. Re-grant it (step 4).
- **`bad CPU type` / app won't launch on an older Mac.** Rebuild with `launcher/build.sh`. Don't copy a binary built somewhere else.
- **`no usable python3 found`.** Install python3, or set `RENPHO_PYTHON` (add an `EnvironmentVariables` dict to the plist).
- **Push errors in the log.** Check the SSH key (`ssh -T git@github.com`). The data is still updated locally, and the push is retried on the next run.


## Files
- **`dashboard.html`** — self-contained interactive dashboard (open in a browser; no server needed). One chart per metric, optional per-chart linear regression, X-axis modes Week / Month / All, per-period delta with good/bad coloring. Embeds `RENPHO Combined-Francisco.csv` (whole-body + segmental) and can load any RENPHO CSV via "Load CSV…". **The only file published to the public repo / GitHub Pages** — see "Publishing to GitHub Pages".
- **`.gitignore`** — ignores everything except `dashboard.html`, so the health data can't be pushed to the public repo by accident.
- **`RENPHO Health-Francisco.csv`** — the main RENPHO export (weight, BMI, body fat, muscle, etc.).
- **`RENPHO Segmental-Francisco.csv`** — complementary CSV with per-segment fat & muscle from the PDF reports. For each of 5 segments (Brazo izquierdo/derecho, Torso, Pierna izquierda/derecha) × {fat, muscle}: mass (lb), % vs standard, and standard (lb). Joins to the main CSV on `No. / Fecha / Hora`.
- **`RENPHO Combined-Francisco.csv`** — main + segmental merged into one file (what the dashboard uses). Regenerated by `process_pdfs.py build`.
- **`segmental_data.jsonl`** — source of truth for the segmental data (one record per report PDF). Edit this, then rebuild.
- **`process_pdfs.py`** — the pipeline (see below). Stdlib only.
- **`ocr_segmental.py`** — reads the segmental numbers + date straight from a report PDF using the local `tesseract` binary (no vision model, no network). Stdlib + tesseract.
- **`pdfimg.py`** — extracts the embedded page image from a RENPHO PDF and writes cropped PNGs. Stdlib only.
- **`renpho-ingest.plist`** — launchd agent that polls the iCloud inbox and runs `ingest` automatically (see "Hands-off ingestion").
- **`RENPHO Ingest.app`** — tiny signed launcher the agent runs so it can be granted Full Disk Access (built from `launcher/` via `sh launcher/build.sh`). Needed because macOS TCC blocks background agents from reading iCloud Drive / Downloads. Built **universal (x86_64 + arm64)** so the same app runs on Apple Silicon and Intel.
- **`reports/`** — the "Report Details" PDFs (each is one measurement in time).

## Reading flat-image PDFs
The RENPHO "Report Details" PDFs are **flat images** — no selectable text. But every report uses the same pixel-stable template, so `ocr_segmental.py` reads the numbers locally with `tesseract`: it crops the two segmental sections + the date header, OCRs each in TSV mode (word + bounding box), and assigns each number to its segment/value slot by position. Validated against all hand-verified records: **750/750 values and every date exact**. If tesseract can't be trusted for a given report (incomplete read or a value that fails the mass ÷ standard consistency check), that PDF is flagged for the manual `scan` path instead of being recorded.

## Hands-off ingestion (the easy path)
Goal: get new measurements from the phone into the dashboard with no file shuffling on the laptop.

**On the phone**, in the RENPHO app export the report PDF and the RENPHO Health CSV, then *Save to Files* → **iCloud Drive → `RENPHO-inbox`**. That's it — they sync to this Mac.

**On the Mac**, a launchd agent (`renpho-ingest.plist`) polls that iCloud folder every 2 minutes and, when something lands, runs `process_pdfs.py ingest`, which moves new report PDFs into `reports/` (de-duplicated by content hash and renamed to the next free number, so a re-synced or same-named file never clobbers or double-counts), **merges** the incoming RENPHO Health CSV into the main CSV (keyed by date+time — so a partial export with only new days *adds/refreshes* rows instead of wiping history; the previous main CSV is backed up under `_work/backups/`), and then runs `auto` (OCR → append → build). Only complete, readable files are committed; a still-syncing file is skipped and picked up on the next poll. Output is logged to `_work/ingest.log`, one timestamped line per event (`[YYYY-MM-DD HH:MM:SS] …`), tracebacks included. Stamping kicks in automatically when output is redirected, so running a command yourself in a terminal stays clean; force it either way with `RENPHO_LOG_TIMESTAMPS=1` / `=0`.

`~/Downloads` is scanned as a fallback too — so **AirDrop still works**: AirDrop the files, then either run `ingest` yourself or let the next poll sweep them in.

### Two things make this work (and why)
1. **Polling, not watching.** iCloud Drive changes are written by Apple's `bird`/`cloudd` daemon and frequently don't emit the fsevents `launchd` `WatchPaths` needs, so the agent polls on a timer (`StartInterval` 120s). `WatchPaths` is kept only as a best-effort fast path.
2. **A signed launcher app for Full Disk Access.** macOS TCC hands *background* agents an empty view of protected folders (iCloud Drive **and** Downloads) with no permission prompt. So the agent doesn't run python directly — it runs **`RENPHO Ingest.app`** (a tiny signed launcher, source in `launcher/`). launchd gives that app a stable identity you grant Full Disk Access to; the app spawns python as a child, which inherits the grant (the same way commands in Terminal inherit Terminal's file access).

### Placeholders: forcing the download
A file in the iCloud inbox may not actually be *on* this Mac — iCloud keeps it as a **placeholder** until something asks for it. `ingest` detects these up front and forces the download before trying to read anything. There are two forms, and both are handled:

- **Dataless** — right name and size, but no bytes locally (carries the `SF_DATALESS` flag). Opening it faults the data in.
- **`.icloud` stub** — the file is *renamed* to a hidden `.<name>.icloud` metadata stub. This one matters: the real name doesn't exist yet, so a plain glob for `Report Details*.pdf` **can't see the file at all**.

For each placeholder, `ingest` runs `brctl download` on it and then **waits** for the bytes to land (up to `DOWNLOAD_TIMEOUT`, 90s per file) before the PDF-parse gate. Both nudges are needed: `brctl` is the only one that works on a `.icloud` stub, while the blocking read is what actually guarantees a dataless file is materialized. Anything that doesn't arrive in time is simply left for the next poll — never a partial read.

### Setup / activation
See **"Setup: run the ingest periodically on a Mac"** at the top. It covers Full Disk Access, loading the agent and day-to-day operation.

### Building the launcher app (Apple Silicon + Intel)
```
sh launcher/build.sh
```
Produces a **universal** `RENPHO Ingest.app` (x86_64 + arm64) so one build runs on Apple Silicon *and* Intel Macs. Three things the build and launcher have to get right for Intel:

- **Architecture.** An arm64-only build fails on Intel with `zsh: bad CPU type in executable`.
- **Deployment target.** `-mmacosx-version-min` is a single global flag, so each arch is compiled separately and merged with `lipo`: x86_64 → **10.13**, arm64 → **11.0** (the earliest macOS on Apple Silicon). Without this the SDK stamps the *current* macOS as the minimum — a binary marked "requires macOS 26" won't launch on an older Mac that can't run it.
- **Homebrew's prefix differs.** It's `/opt/homebrew` on Apple Silicon but `/usr/local` on Intel. The launcher tries `/opt/homebrew/bin/python3`, then `/usr/local/bin/python3`, then `/usr/bin/python3` (needs Command Line Tools), and `$RENPHO_PYTHON` overrides all of them. `PATH` includes both prefixes so `tesseract` resolves either way. The repo path is derived from the app's own location rather than hardcoded, so the checkout can live anywhere.

After rebuilding, **re-grant Full Disk Access**: the bundle is ad-hoc signed, so its TCC identity comes from the binary and macOS treats a rebuilt app as a different one. Remove the old entry and re-add it (step 1 above) on each machine.

Python 3.7+ is required (macOS 12's `/usr/bin/python3` is 3.9 — fine).

Check it's running / tail the log / turn it off:
```
launchctl list | grep renpho-ingest
tail -f ~/repos/renpho/_work/ingest.log
launchctl bootout gui/$(id -u)/com.francisco.renpho-ingest      # disable
```


## Publishing to GitHub Pages
`build` finishes by committing **`dashboard.html`** and pushing it to `origin`, so the Pages site always serves the latest measurements. It's a no-op when the dashboard is byte-identical to what's already committed, so the 2-minute poll doesn't pile up empty commits.

```
python3 process_pdfs.py publish     # push by hand (e.g. after a failed push)
RENPHO_PUBLISH=0 python3 process_pdfs.py build   # rebuild locally, don't push
```

**Only `dashboard.html` is ever pushed.** It's staged by explicit path — never `git add -A` — because everything else here is personal health data: the RENPHO CSVs, `reports/` PDFs, `segmental_data.jsonl`, and the `_work/` logs and backups. `.gitignore` is a deliberate second layer: it ignores `*` and un-ignores only `dashboard.html`, so even a stray `git add -A` can't publish measurements. To publish something else, un-ignore it there explicitly.

Worth knowing, since the repo is public: the dashboard has the full combined dataset **embedded in it**, so every measurement it charts is public and may be indexed or cached by third parties — and git history keeps it even if you later remove it. If that's not what you want, a private repo with Pages (needs a paid plan) or committing an aggregated subset are the two ways out.

Two behaviours worth knowing:
- **Git never blocks the pipeline.** Data is written locally first; a push failure is logged and retried on the next update, and git runs non-interactively (`GIT_TERMINAL_PROMPT=0`, ssh `BatchMode=yes`) with a 120s cap, so a missing credential can never hang the agent waiting for a prompt nobody can answer.
- **The dashboard wins over the remote.** If the remote moved ahead *and* touched `dashboard.html`, rebasing would conflict on every run and the update could never land — it's a generated file. So the fresh build is replayed on top of the remote tip instead. Any other file on the remote (a `_config.yml`, a README) is preserved.

Headless push needs a credential that works with no prompt: an SSH key with no passphrase, or a deploy key with **write** access on the repo (Settings → Deploy keys → *Allow write access*).

### Commit identity
Published commits are authored as `Francisco Candalija <fcandalija@users.noreply.github.com>` — a GitHub noreply address, so no real inbox ends up in a public log while commits still link to the account.

The email is **pinned in `process_pdfs.py`** (`PUBLISH_EMAIL`, passed as `git -c user.email=…`) rather than read from git config, so that whatever a machine's global `~/.gitconfig` says, it can't leak into the public repo. Override with `$RENPHO_GIT_EMAIL`. This repo also sets `user.email` locally for hand-made commits; the global config is left alone.


## Adding report PDFs manually
If you'd rather not use the watcher, the same command works by hand — drop files into the iCloud inbox or `~/Downloads` (or straight into `reports/`) and run:
```
python3 process_pdfs.py ingest   # pull from inbox/Downloads, then auto
# or, if files are already in reports/:
python3 process_pdfs.py auto     # OCR new PDFs, append, build
```
`auto` detects new PDFs, OCRs each, appends the clean reads to `segmental_data.jsonl`, and runs `build`. Anything it can't read confidently is printed with `⚠ needs review` and left for the manual path below. (Requires `tesseract` on PATH — `brew install tesseract`.)

**Only if a report is flagged** (or tesseract isn't available), fall back to the manual read:
```
python3 process_pdfs.py scan
```
This writes `_work/staging/<name>__date.png`, `<name>__sections.png`, and `_work/staging/to_fill.jsonl`.
- Easiest: ask Claude to *"process the new PDFs"* — it reads the crops, appends the records, and runs `build`.
- Manual: read the crops, fill each `[mass_lb, percent_vs_standard, standard_lb]` triple in the skeleton
  (segment keys: `al`=Brazo izquierdo, `ar`=Brazo derecho, `t`=Torso, `ll`=Pierna izquierda, `lr`=Pierna derecha),
  then append the completed lines to `segmental_data.jsonl` and run `build`.

`build` (run automatically by `auto`/`ingest`) regenerates `RENPHO Segmental-Francisco.csv`, merges it with the main CSV into `RENPHO Combined-Francisco.csv`, and embeds that combined data into `dashboard.html` — so just reload the dashboard to see the update. It reports matched/blank rows and flags any gross inconsistency (a shown % that disagrees with mass ÷ standard for larger segments — a likely transcription slip).

Records are matched to reports by **date + time read from each PDF**, not by filename — the filenames are not in CSV order.
