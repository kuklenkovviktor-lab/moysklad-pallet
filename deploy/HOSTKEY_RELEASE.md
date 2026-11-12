# HOSTKEY deployment: preserved production database

This PR does **not** deploy anything automatically. The workflow is `workflow_dispatch` only.

## Current production constraints

- Existing production app: `grit-wms-test-app-1`.
- PostgreSQL service: `grit-wms-test-db-1` (PostgreSQL 17).
- **Actual production database**: `grit_migration_trial_20261111` (historic records).
- Original default `grit` remains present but is **not** the production database.
- Production manager credentials, badge/terminal secrets and MoySklad token live **only** in `/opt/grit-wms/.env`.
- The historical reference database and all current volumes must not be replaced.
- Off-host backups are **not yet confirmed**: do not enable push-triggered deployment or unreviewed database migrations until they are verified.

## One-time HOSTKEY configuration (outside GitHub)

In `/opt/grit-wms/.env`, add or replace exactly one line:

```dotenv
GRIT_DATABASE_NAME=grit_migration_trial_20261111
```

Leave every existing secret in place, including `MOYSKLAD_TOKEN`; never print or commit `.env`. Only after successful release will `GRIT_IMAGE_TAG` be pinned to the released image.

Keep `/opt/grit-data` with adequate disk space. Local pre-deploy backups will be stored at `/opt/grit-data/backups/predeploy`.

## GitHub configuration (outside source control)

Create GitHub Environment `production`; configure reviewer approval if available.
Set **environment secrets**:

- `HOSTKEY_HOST`: HOSTKEY host or IP
- `HOSTKEY_SSH_USER`: account able to run Docker and write to `/opt/grit-wms` and `/opt/grit-data` (current installation uses root; prefer a restricted deploy account subsequently)
- `HOSTKEY_SSH_PRIVATE_KEY`: an **independent dedicated deployment key**, never the user's everyday SSH key
- `HOSTKEY_SSH_KNOWN_HOSTS`: pinned, independently verified SSH host public key line(s), not an unverified `ssh-keyscan` result

The public half of the deploy key must be added to the corresponding server user's `authorized_keys` with least privilege. Do not paste private keys into tickets or chats.

## Release flow

1. Create PR, pass existing CI and review changes.
2. Merge to `main` **only after reviewing production safety**.
3. In GitHub Actions choose **HOSTKEY production release (manual, guarded)** → Run workflow for the intended `main` commit.
4. Workflow runs receiving tests with synthetic credentials, builds an image, sends it over SSH, and invokes `deploy/hostkey_release.sh`.
5. Host script rejects the wrong running database or missing MoySklad token, takes a **local** PostgreSQL custom-format dump, deploys only `app`, checks manager and worker login endpoints, and attempts image rollback on failure.
6. On success it saves the versioned Compose to `/opt/grit-wms/compose.yaml` and pins `GRIT_IMAGE_TAG` in `.env`. No more temporary cutover or rollback overrides for ordinary app releases.

### Limits / outstanding work

- Does not automatically deploy on `push` (intentional until verified off-host backups).
- Does not start or replace the `db` and `backup` containers. The updated Compose sets `backup.PGDATABASE` correctly **for its next controlled restart**, but the currently running backup container may still target `grit`. Schedule its safe reconfiguration and validate an off-host restore.
- HTTP health and tests do not establish semantic correctness of every warehouse workflow.
- Image rollback cannot undo schema changes made by a future application migration. Introduce explicitly reviewed, backwards-compatible migrations before using automated production deploys for schema changes.
- This production config intentionally uses the historic database's current name. Plan a separate controlled rename only after off-host backup and recovery testing.

## Updated release safety gates (2026-11-12)

The branch now requires an explicit `GRIT_DATABASE_NAME` for both app and backup.
On production set `GRIT_PRODUCTION_MODE=1` in the existing `/opt/grit-wms/.env`
**only after** verifying the correct database and after reviewing the merged
release code. With this flag, using `grit` instead of
`grit_migration_trial_20261111` terminates application startup.

The production script requires
`/opt/grit-data/deployment/verified-production-offsite-recovery.txt`.
This is a **human-verified receipt, not automatic evidence**: create it only
after a current production dump was uploaded to remote storage, downloaded
back with integrity verification, and restored in an isolated environment.
The current broken backup of `grit` does NOT qualify. Store the target DB,
UTC verification date, archive SHA256 and evidence location in the receipt
but no passwords, KMS credentials or document data.

Workflow is **manual only** and checks `main` at job level. The testing job
runs the complete Python and JS regressions, builds an image once and uploads
the exact Docker image as a GitHub Actions artifact. Release downloads that
artifact, validates SHA256 and compares Docker image IDs locally and on
HOSTKEY. The host uses `flock` and a versioned release folder, then snapshots
the exact prior Compose references, config and env. On an app failure it
attempts to restore prior image and config; it NEVER restores an old production
database over new warehouse operations.

### First-run operator checklist

1. Verify off-site production recovery, including the real KMS/S3 bucket and
   current PostgreSQL database; record evidence.
2. Configure dedicated SSH deploy key, **independently verified** known_hosts,
   and GitHub Environment `production` with required reviewers and
   branch restriction to main. Configure environment secrets
   HOSTKEY_HOST, HOSTKEY_SSH_USER, HOSTKEY_SSH_PRIVATE_KEY,
   HOSTKEY_SSH_KNOWN_HOSTS. No credentials in repository.
3. Confirm `flock`, Docker, Compose v2, space for an additional local dump,
   and access to versioned /opt/grit-data/deployment/releases.
4. Run a complete isolated deployment and forced-failure rollback rehearsal
   from the candidate SHA. Do not use the production data or real API.
5. Only after those gates, merge reviewed PR to main and use the manual
   release workflow with its production approval.
6. After the first successful release, check that the existing backup
   container still uses its **old target until separately reconfigured**;
   do not confuse an app-only release with fixing the backup service.
7. For subsequent app releases, verify the previous Compose path from the
   app container labels remains in /opt/grit-data/deployment/releases.

**Do not turn on push-triggered production releases until off-site backup
monitoring, proper versioned schema migrations, and rollback matrix have been
accepted separately.**
