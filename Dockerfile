FROM python:3.11-slim

# rasterio/pyproj/netCDF4 ship manylinux wheels with GDAL/PROJ/HDF5/netCDF
# bundled, so most system geo libraries don't need to be installed
# separately here - but GDAL's wheel still dynamically links against the
# system libexpat (used for KML/GML XML parsing), which python:3.11-slim
# doesn't include even though Python's own xml module statically links its
# own copy. Without it, `import rasterio` fails at runtime with
# "ImportError: libexpat.so.1: cannot open shared object file".
RUN apt-get update && apt-get install -y --no-install-recommends libexpat1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY scripts/ scripts/
COPY docker-entrypoint.sh .
RUN chmod +x docker-entrypoint.sh

ENTRYPOINT ["./docker-entrypoint.sh"]
CMD ["--start", "2023-08-01"]
