#!/usr/bin/env python3
"""
Batch export QiFeng-CYGNSS NetCDF wind fields to GeoTIFF format.

Each snapshot is exported as a multi-band GeoTIFF with:
  - Band 1: u10 (eastward wind, m/s)
  - Band 2: v10 (northward wind, m/s)
  - Band 3: ws  (wind speed magnitude, m/s)

The output CRS is EPSG:4326 (WGS84 geographic), with pixel coordinates
converted to lon/lat using the TC center position and 1.5 km grid spacing.

Requirements:
    pip install netCDF4 numpy rasterio

Usage:
    python export_tiff.py --input /path/to/dataset/ --output /path/to/tiff_output/
    python export_tiff.py --input /path/to/IAN_2022.nc --output ./tiff_out/ --storm IAN_2022
"""

import os
import argparse
from pathlib import Path

import numpy as np
import netCDF4 as nc4

try:
    import rasterio
    from rasterio.transform import from_bounds
    from rasterio.crs import CRS
except ImportError:
    raise ImportError("rasterio is required: pip install rasterio")


# Dataset grid parameters
GRID_SIZE = 256
RESOLUTION_KM = 1.5
DOMAIN_KM = GRID_SIZE * RESOLUTION_KM  # 384 km


def km_to_deg_lon(km, lat_deg):
    """Convert km offset to degrees longitude at a given latitude."""
    return km / (111.32 * np.cos(np.radians(lat_deg)))


def km_to_deg_lat(km):
    """Convert km offset to degrees latitude (approx constant)."""
    return km / 110.574


def export_single_snapshot(u10, v10, center_lat, center_lon, out_path):
    """
    Write one snapshot as a 3-band GeoTIFF (u10, v10, wind_speed).

    Parameters
    ----------
    u10 : ndarray (256, 256)
        Eastward wind component in m/s.
    v10 : ndarray (256, 256)
        Northward wind component in m/s.
    center_lat : float
        TC center latitude (deg N).
    center_lon : float
        TC center longitude (deg E).
    out_path : str or Path
        Output file path.
    """
    ws = np.sqrt(u10**2 + v10**2)

    # Compute geographic bounds from the storm-centric grid
    half_domain_km = DOMAIN_KM / 2.0
    lat_extent = km_to_deg_lat(half_domain_km)
    lon_extent = km_to_deg_lon(half_domain_km, center_lat)

    # Row 0 is north, row 255 is south (row index increases southward)
    north = center_lat + lat_extent
    south = center_lat - lat_extent
    west = center_lon - lon_extent
    east = center_lon + lon_extent

    transform = from_bounds(west, south, east, north, GRID_SIZE, GRID_SIZE)

    profile = {
        'driver': 'GTiff',
        'dtype': 'float32',
        'width': GRID_SIZE,
        'height': GRID_SIZE,
        'count': 3,
        'crs': CRS.from_epsg(4326),
        'transform': transform,
        'compress': 'deflate',
        'nodata': np.nan,
    }

    with rasterio.open(str(out_path), 'w', **profile) as dst:
        dst.write(u10.astype(np.float32), 1)
        dst.write(v10.astype(np.float32), 2)
        dst.write(ws.astype(np.float32), 3)
        dst.set_band_description(1, 'u10 (m/s)')
        dst.set_band_description(2, 'v10 (m/s)')
        dst.set_band_description(3, 'wind_speed (m/s)')


def export_storm_file(nc_path, out_dir, ocs_only=True):
    """
    Export all snapshots from one per-TC NetCDF file to GeoTIFF.

    Parameters
    ----------
    nc_path : Path
        Path to a single {STORM}_{YEAR}.nc file.
    out_dir : Path
        Directory for output TIFFs.
    ocs_only : bool
        If True, only export snapshots that pass the OCS quality criterion.

    Returns
    -------
    int
        Number of exported snapshots.
    """
    ds = nc4.Dataset(str(nc_path), 'r')
    storm_name = ds.storm_name
    year = ds.year

    u10 = ds.variables['u10'][:]
    v10 = ds.variables['v10'][:]
    center_lat = ds.variables['center_lat'][:]
    center_lon = ds.variables['center_lon'][:]
    meets_ocs = ds.variables['meets_ocs'][:]

    n_times = u10.shape[0]
    count = 0

    storm_dir = out_dir / f"{storm_name}_{year}"
    storm_dir.mkdir(parents=True, exist_ok=True)

    for t in range(n_times):
        # Skip if OCS filter is on and the snapshot does not meet criterion
        if ocs_only and meets_ocs[t] == 0:
            continue

        # Skip if data is all NaN (missing reconstruction)
        if np.all(np.isnan(u10[t])):
            continue

        fname = f"{storm_name}_{year}_t{t:03d}.tif"
        out_path = storm_dir / fname

        export_single_snapshot(
            u10[t], v10[t],
            float(center_lat[t]), float(center_lon[t]),
            out_path
        )
        count += 1

    ds.close()
    return count


def main():
    parser = argparse.ArgumentParser(
        description="Batch export QiFeng-CYGNSS NetCDF to GeoTIFF"
    )
    parser.add_argument(
        '--input', '-i', type=str, required=True,
        help="Path to dataset directory or a single .nc file"
    )
    parser.add_argument(
        '--output', '-o', type=str, default='./tiff_export',
        help="Output directory for GeoTIFF files (default: ./tiff_export)"
    )
    parser.add_argument(
        '--storm', '-s', type=str, default=None,
        help="Process only specified storm (e.g., IAN_2022). "
             "By default, processes all .nc files in the input directory."
    )
    parser.add_argument(
        '--all-snapshots', action='store_true',
        help="Export all snapshots, including those not meeting OCS criterion"
    )
    args = parser.parse_args()

    input_path = Path(args.input)
    out_dir = Path(args.output)
    out_dir.mkdir(parents=True, exist_ok=True)
    ocs_only = not args.all_snapshots

    # Collect NC files to process
    if input_path.is_file() and input_path.suffix == '.nc':
        nc_files = [input_path]
    elif input_path.is_dir():
        nc_files = sorted(input_path.glob('*.nc'))
        # Exclude ensemble_uncertainty.nc
        nc_files = [f for f in nc_files if 'ensemble' not in f.name.lower()]
    else:
        raise FileNotFoundError(f"Input not found: {input_path}")

    # Filter by storm name if specified
    if args.storm:
        nc_files = [f for f in nc_files if f.stem == args.storm]
        if not nc_files:
            print(f"No file found for storm: {args.storm}")
            return

    total = 0
    for nc_path in nc_files:
        n = export_storm_file(nc_path, out_dir, ocs_only=ocs_only)
        total += n
        print(f"  {nc_path.name}: exported {n} snapshots")

    print(f"\nDone. Total exported: {total} GeoTIFF files -> {out_dir}")


if __name__ == '__main__':
    main()
