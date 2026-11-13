#!/usr/bin/env bash
# Installed once by root at /usr/local/sbin/grit-deploy-received (root:root 0755).
# This is the ONLY sudo entry point for GitHub's restricted gritdeploy account.
# Never executes scripts or shell text from the unprivileged upload directory.
set -Eeuo pipefail
umask 077

sha="${1:-}"; run_id="${2:-}"
[[ "$sha" =~ ^[a-f0-9]{40}$ && "$run_id" =~ ^[0-9]{1,20}$ ]] || {
  echo "Invalid release identifiers"; exit 2;
}
[[ "$#" -eq 2 ]] || exit 2
upload="/home/gritdeploy/releases/$sha-$run_id"
private="/opt/grit-data/deployment/releases/$sha-$run_id"
[[ -d "$upload" && ! -L "$upload" ]] || { echo "Missing release upload"; exit 1; }
[[ ! -e "$private" ]] || { echo "Release already imported"; exit 1; }
[[ "$(stat -c '%U:%G:%a' "$upload")" == "gritdeploy:gritdeploy:700" ]] || {
  echo "Incorrect upload directory ownership or permissions"; exit 1;
}
# Restrict writable area to gritdeploy only and accept regular non-symlink files.
for file in image.tar.gz image.tar.gz.sha256 image.id compose.yaml; do
  [[ -f "$upload/$file" && ! -L "$upload/$file" ]] || { echo "Missing/unsafe file: $file"; exit 1; }
  [[ "$(stat -c '%U' "$upload/$file")" == gritdeploy ]] || { echo "Wrong file owner"; exit 1; }
done
[[ "$(find "$upload" -mindepth 1 -maxdepth 1 -type f | wc -l)" -eq 4 ]] || {
  echo "Unexpected release files"; exit 1;
}
mkdir -p /opt/grit-data/deployment/releases
install -d -m 700 -o root -g root "$private"
for file in image.tar.gz image.tar.gz.sha256 image.id compose.yaml; do
  install -o root -g root -m 600 "$upload/$file" "$private/$file"
done
cd "$private"
[[ "$(wc -l < image.tar.gz.sha256)" -eq 1 ]] || exit 1
expected_sum=$(sed -nE 's/^([a-f0-9]{64})  image\.tar\.gz$/\1/p' image.tar.gz.sha256)
[[ "$expected_sum" =~ ^[a-f0-9]{64}$ ]] || { echo "Bad checksum manifest"; exit 1; }
actual_sum=$(sha256sum image.tar.gz | cut -d' ' -f1)
[[ "$actual_sum" == "$expected_sum" ]] || { echo "Artifact integrity mismatch"; exit 1; }
expected_image=$(cat image.id)
[[ "$expected_image" =~ ^sha256:[a-f0-9]{64}$ ]] || { echo "Bad image ID"; exit 1; }
# Root trusts this image only for a specifically approved GitHub Environment release.
gzip -dc image.tar.gz | docker load
image="grit-wms:release-$sha"
actual_image=$(docker image inspect "$image" --format '{{.Id}}')
[[ "$actual_image" == "$expected_image" ]] || { echo "Image differs from tested artifact"; exit 1; }
# Only the fixed, root-owned deployment script can change production containers.
exec /usr/local/libexec/grit/hostkey_release.sh "$sha" "$run_id"
