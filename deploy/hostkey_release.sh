#!/usr/bin/env bash
# Executed on HOSTKEY as root from the GitHub Actions manual release job.
set -Eeuo pipefail
umask 077

sha="${1:?Missing commit SHA}"
run_id="${2:?Missing GitHub run ID}"
[[ "$run_id" =~ ^[0-9]+$ ]] || { echo "Invalid run ID"; exit 1; }

# Lock across GitHub Actions, manual deploy and other cooperating release scripts.
mkdir -p /opt/grit-data/deployment
exec 9>/opt/grit-data/deployment/production-release.lock
flock -n 9 || { echo "Another production release is in progress"; exit 1; }
[[ "$sha" =~ ^[0-9a-f]{40}$ ]] || { echo "Invalid commit SHA"; exit 1; }

root=/opt/grit-wms
incoming=/opt/grit-data/deployment
release_dir="$incoming/releases/$sha-$run_id"
candidate="$release_dir/compose.yaml"
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
# Explicit production gate. Operator creates this receipt ONLY after a recent
# production backup has been uploaded, downloaded and restored off the VPS.
# Do not satisfy this gate with a backup of the old 'grit' database.
[[ -s /opt/grit-data/deployment/verified-production-offsite-recovery.txt ]] || {
  echo "Blocked: no verified off-site recovery receipt for production database"
  exit 1
}
grep -qx 'GRIT_PRODUCTION_MODE=1' "$envfile" || {
  echo "Set GRIT_PRODUCTION_MODE=1 after verifying production Compose"
  exit 1
}
docker image inspect "$image" >/dev/null
current=$(docker inspect grit-wms-test-app-1 --format '{{.Image}}')
# Keep the exact existing deploy context, including legacy cutover overrides.
config_files=$(docker inspect grit-wms-test-app-1 --format '{{index .Config.Labels "com.docker.compose.project.config_files"}}')
IFS=',' read -r -a old_files <<< "$config_files"
[[ "${#old_files[@]}" -ge 1 ]] || { echo "Cannot identify old Compose context"; exit 1; }
old_args=()
for file in "${old_files[@]}"; do
  [[ -s "$file" ]] || { echo "Missing prior Compose file"; exit 1; }
  old_args+=(-f "$file")
done
# Preserve old .env and base Compose for the complete deployment-context rollback.
cp -p "$envfile" "$release_dir/env.before"
cp -p "$base" "$release_dir/compose.before.yaml"
chmod 600 "$release_dir/env.before" "$release_dir/compose.before.yaml"

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
rollback="$release_dir/rollback.yaml"
cat > "$rollback" <<EOF
services:
  app:
    image: $rollback_image
EOF
chmod 600 "$rollback"

rollback_on_failure() {
  trap - ERR
  echo "Deployment failed: reverting prior app image and Compose context."
  # No database restore here: never delete new operations.
  cp -p "$release_dir/env.before" "$envfile"
  cp -p "$release_dir/compose.before.yaml" "$base"
  if docker compose --project-directory "$root" --env-file "$envfile" \
       "${old_args[@]}" -f "$rollback" up -d --no-deps --no-build --force-recreate app; then
    sleep 5
    state=$(docker inspect grit-wms-test-app-1 --format '{{if .State.Health}}{{.State.Health.Status}}{{end}}')
    echo "Rollback container health: $state"
  else
    echo "ROLLBACK FAILED: manual recovery necessary" >&2
  fi
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
cp "$base" "$release_dir/compose-before-$sha.yaml"
chmod 600 "$release_dir/compose-before-$sha.yaml"
mv "$candidate" "$base"
trap - ERR
echo "DEPLOY PASS: $sha (database=$database)"
