#!/usr/bin/env python3
"""Convert a TEMPO NO2 L2 granule (NetCDF4/HDF5) into a multi-band GeoTIFF.

TEMPO L2 pixels sit on a 2D (mirror_step x xtrack) scan grid with per-pixel
latitude/longitude - not a uniform lat/lon grid - so a direct NetCDF->GeoTIFF
translation isn't possible. Instead each granule is resampled (nearest
neighbor, via pyresample) from its native swath onto a uniform lat/lon grid
covering the Colorado bounding box, and written out as one GeoTIFF with a
band per science variable.

Bands written (see BAND_SPECS below):
    1. no2_vertical_column_troposphere        [molecules/cm^2]
    2. no2_vertical_column_troposphere_uncertainty [molecules/cm^2]
    3. no2_vertical_column_stratosphere        [molecules/cm^2]
    4. no2_vertical_column_total               [molecules/cm^2]
    5. main_data_quality_flag                  (0=normal, 1=suspect, 2=bad)
    6. ground_pixel_quality_flag               (bitmask, see TEMPO L2 ATBD)

All bands are stored as float32 with a single nodata value (NaN) so flag
bands and science bands can share one GeoTIFF - the flag values (small
integers) are exactly representable in float32.

Usage:
    python scripts/tempo_no2_to_geotiff.py granule1.nc granule2.nc ...
    python scripts/tempo_no2_to_geotiff.py --in-dir data/tempo_no2_co --delete-source
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import NamedTuple

import netCDF4
import numpy as np
import rasterio
from pyresample.geometry import AreaDefinition, SwathDefinition
from pyresample.kd_tree import resample_nearest
from rasterio.transform import from_bounds

from tempo_common import CO_BBOX

# Nominal TEMPO ground pixel is ~2.1 x 4.4 km at best; use a grid finer than
# that and a matching search radius so nearest-neighbor resampling doesn't
# leave gaps between adjacent scan lines.
DEFAULT_RESOLUTION_DEG = 0.02
DEFAULT_RADIUS_OF_INFLUENCE_M = 6000

FILL_VALUE_DEFAULT = -1e30
NODATA = np.nan


class BandSpec(NamedTuple):
    path: str  # "<group>/<variable>" in the source file
    band_name: str
    description: str


BAND_SPECS = [
    BandSpec("product/vertical_column_troposphere", "no2_vertical_column_troposphere",
             "NO2 tropospheric vertical column [molecules/cm^2]"),
    BandSpec("product/vertical_column_troposphere_uncertainty", "no2_vertical_column_troposphere_uncertainty",
             "NO2 tropospheric vertical column uncertainty [molecules/cm^2]"),
    BandSpec("product/vertical_column_stratosphere", "no2_vertical_column_stratosphere",
             "NO2 stratospheric vertical column [molecules/cm^2]"),
    BandSpec("support_data/vertical_column_total", "no2_vertical_column_total",
             "NO2 total vertical column [molecules/cm^2]"),
    BandSpec("product/main_data_quality_flag", "main_data_quality_flag",
             "Main data quality flag: 0=normal, 1=suspect, 2=bad"),
    BandSpec("support_data/ground_pixel_quality_flag", "ground_pixel_quality_flag",
             "Ground pixel quality bitmask (see TEMPO L2 NO2 ATBD)"),
]

LAT_PATH = "geolocation/latitude"
LON_PATH = "geolocation/longitude"


def _walk_variables(group):
    for name, var in group.variables.items():
        path = "/".join(p for p in (group.path.strip("/"), name) if p)
        yield path, var
    for sub in group.groups.values():
        yield from _walk_variables(sub)


def find_variable(ds: netCDF4.Dataset, path: str) -> netCDF4.Variable:
    """Look up a variable by its expected group path, falling back to a
    recursive search by short name in case a Harmony subsetting step
    flattened or renamed the group hierarchy."""
    parts = path.split("/")
    node = ds
    try:
        for group_name in parts[:-1]:
            node = node.groups[group_name]
        return node.variables[parts[-1]]
    except KeyError:
        pass

    short_name = parts[-1]
    for full_path, var in _walk_variables(ds):
        if full_path.split("/")[-1] == short_name:
            return var
    raise KeyError(f"variable not found in granule (tried '{path}' and short name '{short_name}')")


def read_masked(var: netCDF4.Variable) -> np.ndarray:
    """Read a variable as float64 with fill values converted to NaN.

    Filled with 0 (not NaN) before the float64 cast so this works for
    integer-typed flag variables too, then NaN'd out by mask afterward.
    """
    data = var[:]
    fill = getattr(var, "_FillValue", FILL_VALUE_DEFAULT)
    if np.ma.isMaskedArray(data):
        mask = np.ma.getmaskarray(data)
        data = np.ma.filled(data, 0).astype("float64")
        data[mask] = np.nan
    else:
        data = data.astype("float64")
    data[data == fill] = np.nan
    return data


def convert_granule(
    nc_path: Path,
    tif_path: Path,
    bbox: tuple[float, float, float, float] = CO_BBOX,
    resolution_deg: float = DEFAULT_RESOLUTION_DEG,
    radius_of_influence_m: float = DEFAULT_RADIUS_OF_INFLUENCE_M,
) -> str:
    """Resample one TEMPO NO2 L2 granule onto a uniform CO grid and write a
    multi-band GeoTIFF.

    Returns "written", "empty" (no valid pixels over the bbox - nothing was
    written), or raises on unexpected failure.
    """
    with netCDF4.Dataset(nc_path, "r") as ds:
        lat = read_masked(find_variable(ds, LAT_PATH))
        lon = read_masked(find_variable(ds, LON_PATH))

        valid = np.isfinite(lat) & np.isfinite(lon)
        west, south, east, north = bbox
        valid &= (lon >= west) & (lon <= east) & (lat >= south) & (lat <= north)
        if not np.any(valid):
            return "empty"

        swath_def = SwathDefinition(lons=np.where(valid, lon, np.nan), lats=np.where(valid, lat, np.nan))

        width = max(1, round((east - west) / resolution_deg))
        height = max(1, round((north - south) / resolution_deg))
        area_def = AreaDefinition(
            "co_grid", "Colorado lat/lon grid", "latlon",
            {"proj": "longlat", "datum": "WGS84"},
            width, height,
            (west, south, east, north),
        )

        bands = []
        for spec in BAND_SPECS:
            var = find_variable(ds, spec.path)
            data = read_masked(var)
            resampled = resample_nearest(
                swath_def, data, area_def,
                radius_of_influence=radius_of_influence_m,
                fill_value=np.nan,
            )
            bands.append(resampled.astype("float32"))

        granule_attrs = {
            "source_file": nc_path.name,
            "time_coverage_start": getattr(ds, "time_coverage_start", ""),
            "time_coverage_end": getattr(ds, "time_coverage_end", ""),
            "resampling": "nearest_neighbor",
            "radius_of_influence_m": str(radius_of_influence_m),
            "grid_resolution_deg": str(resolution_deg),
        }

    if not any(np.any(np.isfinite(b)) for b in bands):
        return "empty"

    transform = from_bounds(west, south, east, north, width, height)
    tif_path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(
        tif_path, "w",
        driver="GTiff",
        height=height, width=width,
        count=len(bands),
        dtype="float32",
        crs="EPSG:4326",
        transform=transform,
        nodata=NODATA,
        compress="deflate",
        predictor=2,
    ) as dst:
        for i, (band, spec) in enumerate(zip(bands, BAND_SPECS), start=1):
            dst.write(band, i)
            dst.set_band_description(i, spec.description)
            dst.update_tags(i, name=spec.band_name)
        dst.update_tags(**granule_attrs)

    return "written"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("files", nargs="*", type=Path, help="Granule .nc file(s) to convert")
    parser.add_argument("--in-dir", type=Path, default=None,
                         help="Recursively convert every .nc file found under this directory")
    parser.add_argument("--tif-dir", type=Path, default=None,
                         help="Output directory for GeoTIFFs (default: alongside each source .nc file)")
    parser.add_argument("--delete-source", action="store_true",
                         help="Delete each .nc file after it's successfully converted (or found empty)")
    parser.add_argument("--resolution-deg", type=float, default=DEFAULT_RESOLUTION_DEG,
                         help=f"Output grid resolution in degrees (default: {DEFAULT_RESOLUTION_DEG})")
    parser.add_argument("--radius-of-influence-m", type=float, default=DEFAULT_RADIUS_OF_INFLUENCE_M,
                         help=f"Nearest-neighbor search radius in meters (default: {DEFAULT_RADIUS_OF_INFLUENCE_M})")
    args = parser.parse_args()

    files = list(args.files)
    if args.in_dir:
        files += sorted(args.in_dir.rglob("*.nc"))
    if not files:
        parser.error("no input files given (pass file paths or --in-dir)")

    written = empty = failed = 0
    for nc_path in files:
        tif_path = (args.tif_dir / nc_path.with_suffix(".tif").name) if args.tif_dir else nc_path.with_suffix(".tif")
        try:
            result = convert_granule(nc_path, tif_path, resolution_deg=args.resolution_deg,
                                      radius_of_influence_m=args.radius_of_influence_m)
            if result == "written":
                written += 1
                print(f"{nc_path.name}: wrote {tif_path}")
            else:
                empty += 1
                print(f"{nc_path.name}: no valid pixels over bbox, skipped")
            if args.delete_source:
                nc_path.unlink(missing_ok=True)
        except Exception as exc:  # noqa: BLE001 - keep going on per-file failures
            failed += 1
            print(f"{nc_path.name}: FAILED - {exc}", file=sys.stderr)

    print(f"\n{written} written, {empty} empty/skipped, {failed} failed (of {len(files)} file(s)).")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
