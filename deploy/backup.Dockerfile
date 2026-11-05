FROM postgres:17-bookworm
COPY --chmod=0555 deploy/backup-*.sh deploy/restore.sh /scripts/
