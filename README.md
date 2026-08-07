# TEMPO NO2 Colorado Downloader

Downloads the full TEMPO NO2 L2 archive, subset to Colorado, using NASA's
Harmony service to clip each granule server-side before it's transferred.
This is what makes downloading years of hourly satellite data feasible in
a few hours instead of requiring multiple terabytes of bandwidth: TEMPO's
L2 granules cover almost all of North America per scan, so without
server-side subsetting "Colorado data" is effectively the entire archive
(~2-4 TB for 2023-2026). Harmony subsetting cuts that by ~95%+.

## 1. Setup

```bash
pip install -r requirements.txt
python scripts/setup_earthdata_auth.py
```

This prompts once for your NASA Earthdata Login (urs.earthdata.nasa.gov)
username/password and saves them to `~/.netrc`. Both `earthaccess` and
`harmony-py` read that file automatically afterwards, so you won't be
prompted again.

If you don't have an Earthdata account yet, create one first at
https://urs.earthdata.nasa.gov/users/new

## 2. Run

```bash
python scripts/download_tempo_no2_co.py --start 2023-08-01 --end 2026-08-07
```

- Splits the date range into one Harmony subsetting job per calendar
  month, and runs several jobs concurrently (`--workers`, default 4).
- Each job asks Harmony to return only pixels inside the Colorado
  bounding box (`scripts/tempo_common.py:CO_BBOX`).
- Files land in `data/tempo_no2_co/<year>/<month>/`.
- Progress and per-month status are checkpointed to
  `data/tempo_no2_co/manifest.json`. If the script is interrupted or a
  month fails, just re-run the same command — completed months are
  skipped and only unfinished/failed months are retried.

Useful flags:

```bash
# Quick test on one month before committing to the full run
python scripts/download_tempo_no2_co.py --start 2024-06-01 --end 2024-06-30

# More concurrent Harmony jobs (faster, but Harmony may throttle/queue)
python scripts/download_tempo_no2_co.py --start 2023-08-01 --end 2026-08-07 --workers 8

# Different TEMPO product (e.g. HCHO) using the same CO bbox pipeline
python scripts/download_tempo_no2_co.py --short-name TEMPO_HCHO_L2 --start 2023-08-01 --end 2026-08-07
```

## Notes / troubleshooting

- **"INVALID REQUEST" errors**: means the resolved collection doesn't
  support Harmony bbox subsetting. Check the error message printed (also
  saved in the manifest) — it usually names the missing capability.
- **Rate limiting / throttling**: Harmony queues jobs server-side; lower
  `--workers` if you see repeated retries.
- **Resuming**: safe to kill (Ctrl-C) and re-run at any time; the
  manifest tracks per-month state (`pending` / `submitted` / `retrying` /
  `done` / `failed`).
- Downloaded data and the manifest are gitignored — this repo holds the
  pipeline code, not the satellite data itself.
