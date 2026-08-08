# TEMPO NO2 Colorado + DJ Basin Downloader

Downloads the full TEMPO NO2 L2 archive, subset to a box covering Colorado
and the Denver-Julesburg (DJ/Julesburg) Basin, using NASA's Harmony service
to clip each granule server-side before it's transferred. This is what makes
downloading years of hourly satellite data feasible in a few hours instead
of requiring multiple terabytes of bandwidth: TEMPO's L2 granules cover
almost all of North America per scan, so without server-side subsetting
"regional data" is effectively the entire archive (~2-4 TB for 2023-2026).
Harmony subsetting cuts that by ~95%+.

Downloaded months are checked into an S3 bucket, which is the pipeline's
source of truth for what's already done. That's what lets the whole thing
be turned off (stopped, killed, machine reclaimed) and back on (re-run,
possibly on a completely fresh machine with an empty local disk) without
ever re-downloading a month that's already in the bucket.

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

**Switching between multiple Earthdata accounts** (e.g. for the
multi-account split in `docs/AWS_DEPLOY.md`): copy
`secrets/earthdata_accounts.json.example` to `secrets/earthdata_accounts.json`
and fill in real usernames/passwords - that file is gitignored, so real
credentials never get committed. Then:

```bash
python scripts/use_earthdata_account.py matthew0011c1
```

writes `~/.netrc` for that account, same as `setup_earthdata_auth.py`
would, just without the interactive prompt.

AWS credentials for the S3 bucket are picked up from the default AWS
credential chain (env vars, `~/.aws/credentials`, an IAM role, etc.) - no
secrets are stored in this repo.

To run this unattended in AWS (self-terminating EC2, optionally split
across multiple Earthdata accounts) instead of on your own machine, see
[`docs/AWS_DEPLOY.md`](docs/AWS_DEPLOY.md).

## 2. Run

```bash
python scripts/download_tempo_no2_co.py --start 2023-08-01 --end 2026-08-07
```

- Splits the date range into one Harmony subsetting job per calendar
  month, and runs several jobs concurrently (`--workers`, default 4).
- Each job asks Harmony to return only pixels inside the Colorado + DJ
  Basin bounding box (`scripts/tempo_common.py:CO_BBOX`).
- **Before submitting a Harmony job for a month, the script checks the S3
  bucket first** (`s3://matt-achem-bucket2/tempo_no2_co/nc/<year>/<month>/`
  and `.../tiffs/<year>/<month>/` by default). If that month's files are
  already there, it's marked done and skipped - no Harmony job, no
  re-download. This is the on/off behavior: stop the script anytime, and
  whenever/wherever it's next run, already-downloaded months are
  recognized from the bucket, not just the local manifest.
- Files land locally in `data/tempo_no2_co/<year>/<month>/` as the raw
  Harmony-subsetted NetCDF granules.
- **Each granule is then regridded onto a fixed 1km x 1km grid** (EPSG:5070,
  Albers Equal-Area, so pixels are true 1km squares) covering the same
  Colorado + DJ Basin bbox, and written as a sibling 4-band GeoTIFF
  (`scripts/regrid_to_geotiff.py`):
  1. `no2_troposphere` - NO2 tropospheric vertical column
  2. `no2_stratosphere` - NO2 stratospheric vertical column
  3. `qc_flag` - main data quality flag
  4. `cloud_fraction` - effective cloud fraction
- Both the raw `.nc` and the regridded `.tif` are uploaded to the S3 bucket,
  split by file type into separate `nc/<year>/<month>/` and
  `tiffs/<year>/<month>/` folders (each still organized by year/month
  underneath). Sort files already in the bucket into this layout with:

  ```bash
  python scripts/sort_bucket_by_filetype.py --dry-run   # preview
  python scripts/sort_bucket_by_filetype.py              # actually move them
  ```
- Progress and per-month status are also checkpointed locally to
  `data/tempo_no2_co/manifest.json` as a fast local cache. If the script is
  interrupted or a month fails, just re-run the same command - completed
  months are skipped (from the bucket if the local manifest is gone) and
  only unfinished/failed months are retried.

Useful flags:

```bash
# Quick test on one month before committing to the full run
python scripts/download_tempo_no2_co.py --start 2024-06-01 --end 2024-06-30

# More concurrent Harmony jobs (faster, but Harmony may throttle/queue)
python scripts/download_tempo_no2_co.py --start 2023-08-01 --end 2026-08-07 --workers 8

# Different TEMPO product (e.g. HCHO) using the same bbox pipeline
python scripts/download_tempo_no2_co.py --short-name TEMPO_HCHO_L2 --start 2023-08-01 --end 2026-08-07

# Point at a different bucket/prefix
python scripts/download_tempo_no2_co.py --bucket my-other-bucket --bucket-prefix tempo_no2

# Disable bucket checks entirely (local manifest only, original behavior)
python scripts/download_tempo_no2_co.py --no-bucket

# Backfill GeoTIFFs for granules already downloaded before this feature
# existed, without re-downloading anything:
python scripts/regrid_to_geotiff.py data/tempo_no2_co
```

## Notes / troubleshooting

- **"INVALID REQUEST" errors**: means the resolved collection doesn't
  support Harmony bbox subsetting. Check the error message printed (also
  saved in the manifest) - it usually names the missing capability.
- **Rate limiting / throttling**: Harmony queues jobs server-side; lower
  `--workers` if you see repeated retries.
- **Resuming ("on/off")**: safe to kill (Ctrl-C) and re-run at any time;
  each month's presence in the S3 bucket is checked before doing any work,
  so a month already uploaded is never redownloaded even if local state
  (manifest, `data/`) is missing. The local manifest tracks finer-grained
  per-month state (`pending` / `submitted` / `retrying` / `done` / `failed`)
  for the current machine.
- **S3 errors**: if the bucket can't be listed/written to (bad credentials,
  missing permissions, bucket doesn't exist), the script raises a clear
  error naming the bucket/prefix. Pass `--no-bucket` to fall back to
  local-only operation.
- **Regridding variable mismatch**: `scripts/regrid_to_geotiff.py` expects
  `product/vertical_column_troposphere`, `product/vertical_column_stratosphere`,
  `product/main_data_quality_flag`, and `support_data/eff_cloud_fraction`
  (plus `geolocation/latitude`/`longitude`) in each granule, with a fallback
  to flattened variable names if Harmony strips NetCDF groups. If a
  granule's actual layout differs, it raises a `KeyError` listing every
  variable actually in the file - update the path constants at the top of
  that script to match.
- **Bucket-first check and new months only**: a month is skipped once
  *any* object exists under its bucket prefix, so months uploaded to the
  bucket before GeoTIFF regridding was added won't automatically get a
  `.tif` on a later run. Backfill those with
  `python scripts/regrid_to_geotiff.py data/tempo_no2_co` (see above) and
  re-upload, or clear/version the bucket prefix if a full reprocess is
  wanted.
- Downloaded data and the manifest are gitignored - this repo holds the
  pipeline code, not the satellite data itself.
