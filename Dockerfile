FROM python:3.11-slim

# rasterio/pyproj/netCDF4 ship manylinux wheels with GDAL/PROJ/HDF5/netCDF
# bundled, so no system geo libraries need to be installed separately here.

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY scripts/ scripts/
COPY docker-entrypoint.sh .
RUN chmod +x docker-entrypoint.sh

ENTRYPOINT ["./docker-entrypoint.sh"]
CMD ["--start", "2023-08-01"]
