# syntax=docker/dockerfile:1
FROM python:3.12-slim-bookworm AS python
RUN --mount=type=secret,id=proxy_ca \
    if [ -f /run/secrets/proxy_ca ]; then export PIP_CERT=/run/secrets/proxy_ca; fi; \
    python -m pip install --no-cache-dir 'psycopg[binary]==3.3.6' 'pip>=26.2.2'
FROM postgres:17-bookworm
COPY --from=python /usr/local /usr/local
WORKDIR /tools
COPY --chmod=0444 deploy/migration/migration_tool.py deploy/migration/ops_entrypoint.py deploy/migration/input_entrypoint.py /tools/
ENV PYTHONPATH=/tools PYTHONDONTWRITEBYTECODE=1
ENTRYPOINT ["python3", "/tools/migration_tool.py"]
