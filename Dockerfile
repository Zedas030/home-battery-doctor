# Battery Doctor
FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 TZ=Europe/Berlin
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY akd/ akd/

# test stage: `docker build --target test .` runs the unit and end-to-end tests
FROM base AS test
COPY requirements-dev.txt .
RUN pip install --no-cache-dir -r requirements-dev.txt
COPY tests/ tests/
COPY config/config.example.yaml config/config.example.yaml
RUN python -m pytest -q -p no:cacheprovider tests/

FROM base AS app
RUN useradd --system --uid 10002 --home /data akd && mkdir -p /data /config /backup && chown akd /data /backup
USER akd
VOLUME ["/data"]
EXPOSE 8104
HEALTHCHECK --interval=60s --timeout=5s --start-period=600s --retries=3 CMD ["python", "-m", "akd.healthcheck"]
ENTRYPOINT ["python", "-m", "akd"]
CMD ["run"]
