# syntax=docker/dockerfile:1
FROM python:3.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY requirements.txt .
# Optional session CA for managed cloud builds; normal VPS builds use system trust.
RUN --mount=type=secret,id=proxy_ca \
    if [ -f /run/secrets/proxy_ca ]; then export PIP_CERT=/run/secrets/proxy_ca; fi; \
    python -m pip install --disable-pip-version-check -r requirements.txt && python -m pip check
COPY --chown=10001:10001 . .
USER 10001:10001
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 CMD ["python", "deploy/healthcheck.py"]
ENTRYPOINT ["python", "deploy/entrypoint.py"]
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--config", "gunicorn.conf.py", "--access-logfile", "-", "--error-logfile", "-", "app:app"]
