#!/usr/bin/env python3
"""Regrid a TEMPO NO2 L2 swath granule onto a fixed 1km x 1km grid GeoTIFF.

TEMPO L2 files are swaths: irregularly spaced pixels (~2-8km footprints
depending on position in the scan), not a regular grid. This module
resamples each granule's pixels onto a fixed, equal-area 1km x 1km grid
(EPSG:5070, CONUS Albers Equal-Area - chosen so pixels are true 1km squares
everywhere in the bbox, unlike plain lat/lon degrees) covering CO_BBOX, and
writes a 4-band GeoTIFF:

    1. no2_troposphere  - NO2 tropospheric vertical column
    2. no2_stratosphere - NO2 stratospheric vertical column
    3. qc_flag          - main_data_quality_flag
    4. cloud_fraction   - effective cloud fraction

Every output GeoTIFF shares the exact same transform/dimensions (the grid
is computed once from CO_BBOX), so files line up pixel-for-pixel across
time for stacking/mosaicking later.

Usage (regrid files already on disk, e.g. to backfill TIFFs for granules
downloaded before this script existed - no re-download needed):

    python scripts/regrid_to_geotiff.py data/tempo_no2_co/2024/06

    # Custom bbox/resolution:
    python scripts/regrid_to_geotiff.py data/tempo_no2_co --resolution-m 1000
"""
from __future__ import annotations

import argparse
import math
import sys
from functools import lru_cache
from pathlib import Path

import netCDF4
import numpy as np
import rasterio
from rasterio.crs import CRS
from rasterio.transform import Affine, from_origin
from pyproj import Transformer
from scipy.spatial import cKDTree

from tempo_common import CO_BBOX

# --- Source variable paths within a TEMPO NO2 L2 granule -------------------
# Written as "<group>/<variable>". If a real granule stores these under
# different names (Harmony subsetting occasionally flattens NetCDF groups),
# _read_var() below falls back to flattened-name variants automatically and
# raises a clear error listing what's actually in the file if nothing matches.
LAT_VAR = "geolocation/latitude"
LON_VAR = "geolocation/longitude"

BANDS = [
    # (band name in output GeoTIFF, source variable path, description)
    ("no2_troposphere", "product/vertical_column_troposphere",
     "NO2 tropospheric vertical column [molecules/cm^2]"),
    ("no2_stratosphere", "product/vertical_column_stratosphere",
     "NO2 stratospheric vertical column [molecules/cm^2]"),
    ("qc_flag", "product/main_data_quality_flag",
     "Main data quality flag (0 = good; see product ATBD)"),
    ("cloud_fraction", "support_data/eff_cloud_fraction",
     "Effective cloud fraction [0-1]"),
]

# Output grid: true 1km x 1km pixels via an equal-area CRS.
GRID_CRS = "EPSG:5070"
GRID_RESOLUTION_M = 1000

# Upper bound on how far a grid cell center may be from the nearest source
# pixel and still take its value. Set to roughly the largest TEMPO L2
# native pixel footprint (pixels grow toward the edge of each scan) so 1km
# grid cells snap to a genuinely nearby sample instead of inventing data far
# from any real pixel.
MAX_SEARCH_DIST_M = 5000


def _list_all_variables(nc, prefix: str = "") -> list[str]:
    names = [f"{prefix}{v}" for v in nc.variables]
    for group_name, group in nc.groups.items():
        names.extend(_list_all_variables(group, prefix=f"{prefix}{group_name}/"))
    return names


def _read_var(nc: netCDF4.Dataset, path: str) -> np.ndarray:
    """Read a variable by '<group>/<name>' path, with flattened-name fallbacks."""
    group_path, _, name = path.rpartition("/")
    candidates = [path]
    if group_path:
        candidates.append(f"{group_path.replace('/', '_')}_{name}")
        candidates.append(name)

    for candidate in candidates:
        parts = candidate.split("/")
        node = nc
        for part in parts[:-1]:
            if part not in node.groups:
                node = None
                break
            node = node.groups[part]
        if node is not None and parts[-1] in node.variables:
            return np.ma.filled(node.variables[parts[-1]][:].astype("float64"), np.nan)

    available = _list_all_variables(nc)
    raise KeyError(
        f"Could not find variable for {path!r} (tried {candidates}). "
        f"Available variables in this file ({len(available)}): {available[:40]}"
        + (" ..." if len(available) > 40 else "")
    )


@lru_cache(maxsize=None)
def build_target_grid(bbox: tuple, resolution_m: int = GRID_RESOLUTION_M, crs: str = GRID_CRS):
    """Compute a fixed 1km grid (transform, width, height) covering bbox in crs.

    Cached so every granule regrids onto byte-identical grid geometry.
    """
    west, south, east, north = bbox
    n = 50
    lons = np.concatenate([
        np.linspace(west, east, n), np.linspace(west, east, n),
        np.full(n, west), np.full(n, east),
    ])
    lats = np.concatenate([
        np.full(n, south), np.full(n, north),
        np.linspace(south, north, n), np.linspace(south, north, n),
    ])
    transformer = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
    xs, ys = transformer.transform(lons, lats)

    x_min = math.floor(xs.min() / resolution_m) * resolution_m
    x_max = math.ceil(xs.max() / resolution_m) * resolution_m
    y_min = math.floor(ys.min() / resolution_m) * resolution_m
    y_max = math.ceil(ys.max() / resolution_m) * resolution_m

    width = int((x_max - x_min) / resolution_m)
    height = int((y_max - y_min) / resolution_m)
    transform = from_origin(x_min, y_max, resolution_m, resolution_m)
    return transform, width, height


def _cell_centers(transform: Affine, width: int, height: int):
    cols = np.arange(width)
    rows = np.arange(height)
    xs = transform.c + (cols + 0.5) * transform.a
    ys = transform.f + (rows + 0.5) * transform.e
    grid_x, grid_y = np.meshgrid(xs, ys)
    return grid_x, grid_y


def _nearest_resample(x_src, y_src, values, grid_x, grid_y, max_dist_m: float) -> np.ndarray:
    tree = cKDTree(np.column_stack([x_src, y_src]))
    grid_pts = np.column_stack([grid_x.ravel(), grid_y.ravel()])
    dist, idx = tree.query(grid_pts, k=1, distance_upper_bound=max_dist_m)

    out = np.full(grid_pts.shape[0], np.nan, dtype="float32")
    valid = np.isfinite(dist)
    out[valid] = values[idx[valid]].astype("float32")
    return out.reshape(grid_x.shape)


def regrid_granule(
    nc_path: Path,
    tif_path: Path,
    bbox: tuple = CO_BBOX,
    resolution_m: int = GRID_RESOLUTION_M,
    crs: str = GRID_CRS,
) -> Path:
    """Read one TEMPO NO2 L2 granule and write a 4-band 1km-grid GeoTIFF."""
    with netCDF4.Dataset(nc_path) as nc:
        lat = _read_var(nc, LAT_VAR).ravel()
        lon = _read_var(nc, LON_VAR).ravel()
        band_values = {name: _read_var(nc, path).ravel() for name, path, _ in BANDS}

    valid_geo = np.isfinite(lat) & np.isfinite(lon)
    lat, lon = lat[valid_geo], lon[valid_geo]
    for name in band_values:
        band_values[name] = band_values[name][valid_geo]

    transformer = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
    x_src, y_src = transformer.transform(lon, lat)

    transform, width, height = build_target_grid(tuple(bbox), resolution_m, crs)
    grid_x, grid_y = _cell_centers(transform, width, height)

    gridded = {
        name: _nearest_resample(x_src, y_src, values, grid_x, grid_y, MAX_SEARCH_DIST_M)
        for name, values in band_values.items()
    }

    tif_path.parent.mkdir(parents=True, exist_ok=True)
    profile = dict(
        driver="GTiff",
        height=height,
        width=width,
        count=len(BANDS),
        dtype="float32",
        crs=CRS.from_string(crs),
        transform=transform,
        nodata=np.nan,
        compress="deflate",
        predictor=2,
        tiled=True,
    )
    with rasterio.open(tif_path, "w", **profile) as dst:
        for i, (name, _, description) in enumerate(BANDS, start=1):
            dst.write(gridded[name], i)
            dst.set_band_description(i, description)

    return tif_path


def regrid_file(nc_path: Path, bbox: tuple = CO_BBOX) -> Path:
    """Regrid nc_path to a sibling .tif with the same basename. Skips if up to date."""
    tif_path = nc_path.with_suffix(".tif")
    if tif_path.exists() and tif_path.stat().st_mtime >= nc_path.stat().st_mtime:
        return tif_path
    return regrid_granule(nc_path, tif_path, bbox=bbox)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("path", type=Path, help="A granule .nc file, or a directory to search recursively")
    parser.add_argument("--resolution-m", type=int, default=GRID_RESOLUTION_M, help="Grid resolution in meters")
    args = parser.parse_args()

    if args.path.is_file():
        nc_files = [args.path]
    else:
        nc_files = sorted(args.path.rglob("*.nc")) + sorted(args.path.rglob("*.nc4"))

    if not nc_files:
        print(f"No .nc/.nc4 files found under {args.path}", file=sys.stderr)
        return 1

    failures = 0
    for nc_path in nc_files:
        try:
            tif_path = regrid_file(nc_path)
            print(f"{nc_path} -> {tif_path}")
        except Exception as exc:  # noqa: BLE001 - report and continue with the rest of the batch
            print(f"{nc_path}: FAILED - {exc}", file=sys.stderr)
            failures += 1

    print(f"\nDone. {len(nc_files) - failures}/{len(nc_files)} granule(s) regridded.")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
