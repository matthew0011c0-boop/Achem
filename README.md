# TEMPO NO2 Colorado GeoTIFF Pipeline

Downloads the TEMPO NO2 L2 V04 archive, subset to Colorado, using NASA's
Harmony service to clip each granule server-side before it's transferred,
then converts every granule to a multi-band GeoTIFF and deletes the source
NetCDF. This is what makes downloading years of hourly satellite data
feasible without needing terabytes of disk: TEMPO's L2 granules cover
almost all of North America per scan, so without server-side subsetting
"Colorado data" is effectively the entire archive (~2-4 TB for 2023-2026).
Harmony subsetting cuts that by ~95%+, and converting straight to GeoTIFF
(deleting the .nc right after) keeps disk usage bounded to about one
month's worth of granules at a time instead of the whole archive.

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
  bounding box (`scripts/tempo_common.py:CO_BBOX`), from the
  `TEMPO_NO2_L2` **V04** collection (`scripts/tempo_common.py:DEFAULT_VERSION`).
- For every downloaded granule: resamples it onto a uniform Colorado
  lat/lon grid and writes a multi-band GeoTIFF (see "GeoTIFF contents"
  below), then **deletes the source `.nc` file**. Pass `--keep-nc` to keep
  the raw NetCDF files instead.
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

# Keep the raw .nc files around instead of deleting them post-conversion
python scripts/download_tempo_no2_co.py --start 2023-08-01 --end 2026-08-07 --keep-nc

# Coarser/finer output grid and resampling search radius
python scripts/download_tempo_no2_co.py --start 2023-08-01 --end 2026-08-07 \
    --resolution-deg 0.02 --radius-of-influence-m 6000

# Different TEMPO product/version using the same CO bbox pipeline
python scripts/download_tempo_no2_co.py --short-name TEMPO_HCHO_L2 --version V03 --start 2023-08-01 --end 2026-08-07
```

## GeoTIFF contents

TEMPO L2 pixels sit on a 2D scan grid with per-pixel lat/lon (not a
uniform grid), so each granule is resampled (nearest-neighbor, via
`pyresample`) onto a uniform lat/lon grid over Colorado and written as one
GeoTIFF (`EPSG:4326`) with 6 float32 bands, NaN nodata:

| Band | Name                                    | Description                                   |
|------|------------------------------------------|------------------------------------------------|
| 1    | `no2_vertical_column_troposphere`        | NO2 tropospheric vertical column [molecules/cm^2] |
| 2    | `no2_vertical_column_troposphere_uncertainty` | Uncertainty on band 1 [molecules/cm^2]   |
| 3    | `no2_vertical_column_stratosphere`       | NO2 stratospheric vertical column [molecules/cm^2] |
| 4    | `no2_vertical_column_total`              | NO2 total vertical column [molecules/cm^2]    |
| 5    | `main_data_quality_flag`                 | 0 = normal, 1 = suspect, 2 = bad              |
| 6    | `ground_pixel_quality_flag`               | Ground pixel quality bitmask (see TEMPO L2 NO2 ATBD) |

Filter on bands 5/6 before using bands 1-4 for analysis — TEMPO NO2 L2
granules routinely include suspect/bad pixels (cloud, glint, terminator,
etc.) that shouldn't be averaged in uncritically.

Granule metadata (source filename, acquisition time window, resampling
parameters) is stored as GeoTIFF tags — inspect with
`gdalinfo <file>.tif` or `rasterio.open(...).tags()`.

To (re)convert granules you've already downloaded, or reprocess with
different resampling settings, run the converter directly:

```bash
python scripts/tempo_no2_to_geotiff.py --in-dir data/tempo_no2_co --delete-source
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
- **Empty GeoTIFFs**: a granule with zero valid pixels inside the Colorado
  bbox after resampling (e.g. nighttime scans) is skipped — no `.tif` is
  written for it, and the manifest's `n_empty` count reflects how many
  were skipped per month.
- Downloaded data, GeoTIFFs, and the manifest are gitignored — this repo
  holds the pipeline code, not the satellite data itself.
