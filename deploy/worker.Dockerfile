ARG APP_IMAGE
FROM ${APP_IMAGE}
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 CMD ["python", "/app/wb_worker_health.py"]
CMD ["python", "-u", "/app/wb_fbs_sync_v11.py"]
