# syntax=docker/dockerfile:1
# Isolated test emulator only. Never deploy this image or use real S3 keys here.
FROM python:3.12-slim-bookworm
RUN --mount=type=secret,id=proxy_ca \
    if [ -f /run/secrets/proxy_ca ]; then export PIP_CERT=/run/secrets/proxy_ca; fi; \
    python -m pip install --no-cache-dir 'moto[server]==5.1.17'
CMD ["moto_server", "-H", "0.0.0.0", "-p", "5000", "--ssl-cert", "/tls/cert.pem", "--ssl-key", "/tls/key.pem"]
