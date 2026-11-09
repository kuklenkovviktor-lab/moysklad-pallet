# Preserving issued employee badges during HOSTKEY startup

Employee login authenticates against stored `employees.badge_code` or
`scanner_code`. HMAC generation is for issuance; it must not silently rotate
already issued credentials when the application starts with a different key.

Startup now fills only NULL/empty credential fields for existing seed employees.
New employees still receive generated credentials. Existing employee settings,
inactive status, issued codes and login access checks remain unchanged.
The bootstrap badge page uses the stored code and substitutes its QR payload.
The manager print endpoint continues to use stored issued codes.

## Validation

`tests/test_badge_preservation.py` uses synthetic retired/current keys and five
synthetic employees. It checks repeated initialization, real QR/scanner login,
partial missing credentials, fresh seeds, inactive-user refusal and printing.
The dedicated workflow runs the suite on SQLite and PostgreSQL 17. No production
credentials, historical employee badge values or database archives enter CI.

Optional PostgreSQL testing accepts `GRIT_BADGE_TEST_ADMIN_DSN` only for a
loopback PostgreSQL maintenance database and creates fresh `badge_ci_*` fixtures.
It never drops an existing database.

## Approval gate and update

Merge approval is separate from operational approval. Do not merge/deploy as
part of a migration rehearsal. Before an approved working-container update:

1. Confirm the exact application database and that no database/DNS cutover is
   included. Record container/image IDs, commit, env/Compose files and networks.
2. Make root-only, read-back-verified configuration backups. Create and validate
   a fresh Custom backup of that exact database; preserve issued employee codes
   in a private comparison manifest without writing them to public logs.
3. Build an immutable app image from the approved merge commit. Verify the image
   on a separate restored database with the approved source signing keys and
   with real integrations disabled and no published ports.
4. Require all issued QR/scanner logins, unchanged employee settings and repeat
   initialization to pass. Accept only explicitly reviewed startup schema deltas.
5. Prepare a separate root-only candidate configuration. Validate the three
   resolved key values in memory and retain every unrelated setting. Do not
   replace working secrets until the owner explicitly authorizes the update.
6. In the agreed window, update only the app container using its recorded full
   Compose configuration and pinned image. Never restart/recreate DB or backup,
   select the default `grit` accidentally, or alter DNS/integration settings.
7. Check startup/DB readiness, stored-code equality and controlled auth checks;
   record the result and verify the backup still targets the intended database.

## Rollback

Before new business writes, an approved failed app-only update can be stopped
and its old image/configuration restored only after confirming that startup of
the old code with its old key will not overwrite the retained issued codes.
Do not blindly restart an old image: its unconditional reseeding was the defect.
If rollback would rotate codes, keep the app stopped and use a fixed image or
an agreed safe recovery procedure instead. Keep all backups and databases.

After new business writes, preserve the current database and external outcomes.
Do not restore an older dump or return to an obsolete Render database. Stop new
writes only with approval, take a fresh backup and reconcile/replay any recovery
tail. Database restore, reverse migration, DNS changes and integration activation
require separate authorization.
