# syntax=docker/dockerfile:1
FROM python:3.12-slim-bookworm AS python
RUN --mount=type=secret,id=proxy_ca \
    if [ -f /run/secrets/proxy_ca ]; then export PIP_CERT=/run/secrets/proxy_ca; fi; \
    python -m pip install --no-cache-dir boto3==1.40.61 'certifi>=2026.10.19' 'pip>=26.2.2'
FROM postgres:17-bookworm
# Both stages use Debian bookworm. Keep PostgreSQL 17 client tools from this base.
COPY --from=python /usr/local /usr/local
COPY --chmod=0555 deploy/backup-*.sh deploy/restore.sh deploy/backup_worker.py deploy/fetch_s3.py /scripts/
COPY --chmod=0555 deploy/migration/copy_bundle_s3.py /scripts/copy_bundle_s3.py
