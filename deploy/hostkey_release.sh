#!/usr/bin/env bash
# Executed on HOSTKEY as root from the GitHub Actions manual release job.
set -Eeuo pipefail
umask 077

sha="${1:?Missing commit SHA}"
[[ "$sha" =~ ^[0-9a-f]{40}$ ]] || { echo "Invalid commit SHA"; exit 1; }

root=/opt/grit-wms
incoming=/opt/grit-data/deployment
candidate="$root/compose.next.yaml"
base="$root/compose.yaml"
envfile="$root/.env"
image="grit-wms:release-$sha"
database=grit_migration_trial_20261111
backup_dir=/opt/grit-data/backups/predeploy
mkdir -p "$backup_dir"
chmod 700 "$backup_dir"

[[ -s "$candidate" && -s "$envfile" && -s "$base" ]] || {
  echo "Missing existing Compose, candidate Compose or .env"; exit 1;
}
grep -qx "GRIT_DATABASE_NAME=$database" "$envfile" || {
  echo "Production database not explicitly selected in .env"; exit 1;
}
grep -q '^MOYSKLAD_TOKEN=.' "$envfile" || {
  echo "Missing MoySklad token in .env"; exit 1;
}
docker image inspect "$image" >/dev/null
current=$(docker inspect grit-wms-test-app-1 --format '{{.Image}}')
old_db=$(docker inspect grit-wms-test-app-1 --format '{{range .Config.Env}}{{println .}}{{end}}' | sed -n 's/^GRIT_DATABASE_NAME=//p')
[[ "$old_db" == "$database" ]] || {
  echo "Running application is not on expected database; aborting"; exit 1;
}

# Verify the candidate is the intended app and backup database, without logging secrets.
GRIT_IMAGE_TAG="release-$sha" docker compose --project-directory "$root" \
  --env-file "$envfile" -f "$candidate" config --format json | \
  python3 -c 'import json,sys; x=json.load(sys.stdin)["services"]; assert x["app"]["environment"]["GRIT_DATABASE_NAME"]=="grit_migration_trial_20261111"; assert x["app"]["environment"]["MOYSKLAD_TOKEN"]; assert x["backup"]["environment"]["PGDATABASE"]=="grit_migration_trial_20261111"; assert x["app"]["image"]==sys.argv[1]' "$image"

# Backup source database with PostgreSQL 17 tools; no backup leaves server.
stamp=$(date -u +%Y%m%dT%H%M%SZ)
tmp="$backup_dir/.predeploy-$stamp-$sha.partial"
dump="$backup_dir/predeploy-$stamp-$sha.dump"
trap 'rm -f "$tmp"' EXIT
echo "Creating local pre-deploy PostgreSQL backup"
docker exec grit-wms-test-db-1 pg_dump -U grit -d "$database" -Fc --no-owner --no-acl > "$tmp"
[[ -s "$tmp" ]] || { echo "Empty backup; refusing deploy"; exit 1; }
docker exec -i grit-wms-test-db-1 pg_restore -l < "$tmp" > /dev/null
mv "$tmp" "$dump"
sha256sum "$dump" > "$dump.sha256"
echo "Pre-deploy backup saved locally"

# Keep the old image for rollback; don't rely on a mutable image tag.
rollback_image="grit-wms:rollback-$sha"
docker tag "$current" "$rollback_image"
rollback="$incoming/rollback-$sha.yaml"
cat > "$rollback" <<EOF
services:
  app:
    image: $rollback_image
EOF
chmod 600 "$rollback"

rollback_on_failure() {
  echo "Deployment failed. Attempting application image rollback."
  GRIT_IMAGE_TAG="release-$sha" docker compose --project-directory "$root" \
    --env-file "$envfile" -f "$candidate" -f "$rollback" \
    up -d --no-deps --no-build --force-recreate app || true
}
trap rollback_on_failure ERR

echo "Deploying tested image to existing database"
GRIT_IMAGE_TAG="release-$sha" docker compose --project-directory "$root" \
  --env-file "$envfile" -f "$candidate" \
  up -d --no-deps --no-build --force-recreate app

healthy=0
for _ in $(seq 1 60); do
  state=$(docker inspect grit-wms-test-app-1 --format '{{if .State.Health}}{{.State.Health.Status}}{{end}}')
  if [[ "$state" == healthy ]]; then healthy=1; break; fi
  sleep 3
done
[[ "$healthy" == 1 ]] || { echo "App failed healthcheck"; false; }
curl -fsS --max-time 10 -o /dev/null http://127.0.0.1:8000/manager-login
curl -fsS --max-time 10 -o /dev/null http://127.0.0.1:8000/login

# Promote config and pin image for future ordinary 'docker compose up'.
python3 - "$envfile" "release-$sha" <<'PY'
from pathlib import Path
import os, sys, tempfile
path=Path(sys.argv[1]); tag=sys.argv[2]
lines=path.read_text().splitlines()
lines=[line for line in lines if not line.startswith("GRIT_IMAGE_TAG=")]
lines.append("GRIT_IMAGE_TAG="+tag)
fd,name=tempfile.mkstemp(prefix=".env-",dir=path.parent)
try:
    os.fchmod(fd,0o600)
    with os.fdopen(fd,"w") as f:
        f.write("\n".join(lines)+"\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(name,path)
finally:
    if os.path.exists(name): os.unlink(name)
PY
cp "$base" "$incoming/compose-before-$sha.yaml"
chmod 600 "$incoming/compose-before-$sha.yaml"
mv "$candidate" "$base"
trap - ERR
echo "DEPLOY PASS: $sha (database=$database)"
