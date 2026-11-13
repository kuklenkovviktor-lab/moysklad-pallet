# GRIT HOSTKEY: first release bootstrap

## What is ready in GitHub

- `HOSTKEY production release (manual, guarded)` is **manual** and only runs from `main`.
- `delivery-only` transfers the image to `/home/gritdeploy/releases/<sha>-<run_id>` and validates its digest, without touching running GRIT.
- `deploy` additionally calls **only** `sudo -n /usr/local/sbin/grit-deploy-received <sha> <run_id>`.
- `gritdeploy` is NOT added to the Docker group and has no unrestricted sudo.
- The root-owned dispatcher imports the image only after validation, and calls the root-owned production release guard.

## One-time on HOSTKEY as root, AFTER delivery-only workflow succeeds

1. Find the run's SHA and run ID. This is the exact folder shown in GitHub Actions:

   `/home/gritdeploy/releases/<sha>-<run_id>`

2. Inspect both proposed script files and review their checksums. They are untrusted until inspected.
   Never `sudo bash` a file from the writable inbox.

3. Install ONLY the reviewed scripts as root-owned files (replace placeholders):

```bash
SHA='<40-hex-commit-sha>'
RUN_ID='<numeric-actions-run-id>'
SOURCE="/home/gritdeploy/releases/$SHA-$RUN_ID"
test -f "$SOURCE/grit-deploy-received.sh" && test -f "$SOURCE/hostkey_release.sh" || exit 1
install -d -o root -g root -m 755 /usr/local/libexec/grit
install -o root -g root -m 755 "$SOURCE/grit-deploy-received.sh" /usr/local/sbin/grit-deploy-received
install -o root -g root -m 755 "$SOURCE/hostkey_release.sh" /usr/local/libexec/grit/hostkey_release.sh
bash -n /usr/local/sbin/grit-deploy-received /usr/local/libexec/grit/hostkey_release.sh
```

4. Only after verifying those files are root-owned, set the restricted sudoers entry:

```bash
printf '%s\n' 'gritdeploy ALL=(root) NOPASSWD: /usr/local/sbin/grit-deploy-received *' \
  > /etc/sudoers.d/gritdeploy-release
chmod 440 /etc/sudoers.d/gritdeploy-release
visudo -cf /etc/sudoers.d/gritdeploy-release
```

5. Verify the **negative test** without any release:
   `sudo -u gritdeploy sudo -n /usr/bin/docker ps` must fail.
   `sudo -u gritdeploy sudo -n /usr/local/sbin/grit-deploy-received invalid 1`
   must fail with `Invalid release identifiers`.

## Additional production gates

The production release **must stay blocked** until all conditions below hold:

- The real PostgreSQL database is `grit_migration_trial_20261111`. The old database `grit` must not be chosen.
- A current real production dump has been recovered from off-site storage into a temporary PostgreSQL database, and compared with source; only then create
  `/opt/grit-data/deployment/verified-production-offsite-recovery.txt`.
- `/opt/grit-wms/.env` has `GRIT_PRODUCTION_MODE=1`, and the exact expected `GRIT_DATABASE_NAME`.
- Production contains migration overrides for `compose.app-rollback.json`, `grit-cutover-20261111.yaml`, and `grit-moysklad.yaml`; preserve and test rollback for those actual Compose files.
- Confirm the backup service has been reconfigured separately; app-only release does not silently fix the existing backup target.
- Review protections for GitHub Environment `production` and its permitted branch `main`.
- Confirm all GitHub Actions checks green and that prior badge fix PR #102 is present in target branch.
- Perform deployment/rollback rehearsal in separate synthetic environments first.

### Operational notes

The root dispatcher checks only uploaded hash and image ID; **those values do not by themselves prove GitHub signing or authorship**.
Security relies on the protected `main` branch, restricted GitHub Actions `production` Environment,
scoped SSH key, and human release approval. The dispatcher cannot safely confer Docker's root privileges
without trusting the approved image source. Never deploy arbitrary unreviewed images.

**No automatic push-to-production.** After the protected release has proved stable,
a separately reviewed change can add auto-promotion for approved environments.
