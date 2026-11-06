# HOSTKEY: ручное обновление PR №92–96 без замены PostgreSQL

**Это сценарий для владельца после review, не автоматический deploy.** Он не
переносит Render-данные, не переключает DNS и не включает публичный HTTPS.
Сначала проверить внешний CI по точным SHA и согласовать окно остановки тестовой
HOSTKEY WMS. Пока рабочая WMS на Render, её сервисы/БД не трогать.

До объединения PR проверить Auto Deploy Render: merge в main может косвенно
запустить production deployment. Если это возможно, не объединять main до
отдельного согласования управления Auto Deploy. Разработчик его не менял.

## Условия остановки сценария

Остановиться, если рабочий каталог грязный, база/проект/compose overlays отличаются
от ожидаемых, нет свободного места, не совпадают архив/данные, не готов app,
неуспешны mandatory S3/Telegram или обнаружены дубли/orphan/unresolved операции.
Не пытаться «исправить» ситуацию удалением БД/архивов, новым init_env.py,
перегенерацией ключей или сменой POSTGRES_PASSWORD.

Сценарий ниже рассчитан на Ubuntu HOSTKEY, существующий проект `/opt/grit-wms`,
сервисы app/db/backup и базу grit на `/opt/grit-data/postgres`. Если уже активны
другие compose overlays (например HTTPS), сначала нужен адаптированный план.
Не угадывать project name и не запускать второй PostgreSQL на том же bind path.

## 1. Идентификация и защищённая точка сохранения

Команды выполняются в одном Bash-сеансе на VPS владельцем с Docker/root-доступом.
После разрыва сеанса не продолжать с середины: сначала восстановить контекст
из защищённого GRIT_STATE и повторить диагностику, а не повторять deployment вслепую. Не включать
`set -x`, не отправлять вывод `.env`/полного inspect/compose config в чат.

```bash
set -euo pipefail
umask 077
cd /opt/grit-wms
unset DOCKER_HOST DOCKER_CONTEXT DOCKER_TLS DOCKER_TLS_VERIFY DOCKER_CERT_PATH
GRIT_DOCKER=(docker --host=unix:///var/run/docker.sock)
test -f .env
test -z "$(git status --porcelain)"  # иначе сохранить и разобрать изменения
GRIT_OLD_COMMIT=$(git rev-parse HEAD)
GRIT_DB_ID=$("${GRIT_DOCKER[@]}" compose -f compose.yaml ps -q db)
test -n "$GRIT_DB_ID"
GRIT_PROJECT=$("${GRIT_DOCKER[@]}" inspect --format '{{index .Config.Labels "com.docker.compose.project"}}' "$GRIT_DB_ID")
GRIT_FILES=$("${GRIT_DOCKER[@]}" inspect --format '{{index .Config.Labels "com.docker.compose.project.config_files"}}' "$GRIT_DB_ID")
test "$GRIT_FILES" = '/opt/grit-wms/compose.yaml'
GRIT_DB_MOUNT=$("${GRIT_DOCKER[@]}" inspect --format '{{range .Mounts}}{{if eq .Destination "/var/lib/postgresql/data"}}{{.Source}}{{end}}{{end}}' "$GRIT_DB_ID")
test "$GRIT_DB_MOUNT" = '/opt/grit-data/postgres'
test "$("${GRIT_DOCKER[@]}" inspect --format '{{.State.Health.Status}}' "$GRIT_DB_ID")" = healthy

grit_dc() {
  "${GRIT_DOCKER[@]}" compose --project-directory /opt/grit-wms \
    --env-file /opt/grit-wms/.env -p "$GRIT_PROJECT" -f /opt/grit-wms/compose.yaml "$@"
}
GRIT_PG_VERSION=$(grit_dc exec -T db psql -U grit -d grit -Atc 'SHOW server_version_num')
case "$GRIT_PG_VERSION" in 17????) ;; *) echo 'Expected PostgreSQL 17; stop' >&2; exit 1;; esac

test -d /opt/grit-data/backups
if ! test -d /opt/grit-data/rollouts; then mkdir -m 700 /opt/grit-data/rollouts; fi
GRIT_STAMP=$(date -u +%Y%m%d_%H%M%S)
GRIT_STATE=$(mktemp -d "/opt/grit-data/rollouts/update_${GRIT_STAMP}_XXXXXX")
GRIT_BUNDLE_ROOT=$(mktemp -d "/opt/grit-data/backups/preupdate_${GRIT_STAMP}_XXXXXX")
export GRIT_OPERATIONS_DIR="$GRIT_BUNDLE_ROOT"
export GRIT_STATE
printf '%s\n' "$GRIT_PROJECT" > "$GRIT_STATE/project"
printf '%s\n' "$GRIT_DB_ID" > "$GRIT_STATE/db.container"
printf '%s\n' "$GRIT_OLD_COMMIT" > "$GRIT_STATE/source.commit"
install -m 600 .env "$GRIT_STATE/env.saved"
if test -f .env.backup; then install -m 600 .env.backup "$GRIT_STATE/env.backup.saved"; fi
install -m 600 compose.yaml "$GRIT_STATE/compose.before.yaml"
for GRIT_SERVICE in app db backup; do
  GRIT_CONTAINER=$(grit_dc ps -q "$GRIT_SERVICE")
  test -n "$GRIT_CONTAINER"
  "${GRIT_DOCKER[@]}" inspect --format '{{.Image}}' "$GRIT_CONTAINER" > "$GRIT_STATE/${GRIT_SERVICE}.image"
done
```

Сохраняются файлы, а не значения секретов в терминале. Копии env лежат отдельно
от операций/архивов, не монтируются в ops. Если project/config_files не совпали,
ничего не поднимать с новым именем — сначала выяснить текущую конфигурацию.

## 2. Freeze только HOSTKEY и независимый аварийный dump

Пользователи/интеграции HOSTKEY не должны писать до конца проверки. Render не
останавливается: его данные в этом сценарии не читаются и не импортируются.
Остановить только app и старый backup. Старый backup может иметь pruning, поэтому
для страховки НЕ запускать его backup-once.sh.

```bash
grit_dc stop app backup
grit_dc exec -T db pg_dump -U grit -d grit --format=custom --no-owner --no-acl \
  > "$GRIT_BUNDLE_ROOT/emergency.dump.partial"
grit_dc exec -T db pg_restore --list \
  < "$GRIT_BUNDLE_ROOT/emergency.dump.partial" > "$GRIT_BUNDLE_ROOT/emergency.list"
mv "$GRIT_BUNDLE_ROOT/emergency.dump.partial" "$GRIT_BUNDLE_ROOT/emergency.dump"
(cd "$GRIT_BUNDLE_ROOT" && sha256sum emergency.dump > emergency.sha256 && sha256sum -c emergency.sha256)
```

Если dump/list/checksum неуспешны — остановиться; не удалять partial/предыдущие
архивы. Проверить отсутствие других writers. Никаких `docker compose down`,
`down -v`, `rm -rf`, SQL DROP DATABASE/TRUNCATE или копирования живого PGDATA.

## 3. Выбрать точный проверенный release и собрать новые образы

Ниже GRIT_APPROVED_COMMIT нужно явно задать на проверенный SHA, а не HEAD/latest.
Предпочтительно это итоговый main после согласованного объединения PR. Для
изолированного HOSTKEY теста допустим review-approved integration SHA по отдельному
решению владельца. Пример намеренно не подставляет текущий SHA автоматически.

```bash
: "${GRIT_APPROVED_COMMIT:?Set the exact reviewed release SHA first}"
[[ "$GRIT_APPROVED_COMMIT" =~ ^[0-9a-f]{40}$ ]] || { echo 'Use a full immutable commit SHA, not a branch/tag' >&2; exit 1; }
git fetch origin
git cat-file -e "${GRIT_APPROVED_COMMIT}^{commit}"
git switch --detach "$GRIT_APPROVED_COMMIT"
test "$(git rev-parse HEAD)" = "$GRIT_APPROVED_COMMIT"
test -f compose.ops.yaml
cmp -s .env "$GRIT_STATE/env.saved"
if test -f "$GRIT_STATE/env.backup.saved"; then cmp -s .env.backup "$GRIT_STATE/env.backup.saved"; fi
grit_dc config --quiet

grit_ops_compose() {
  "${GRIT_DOCKER[@]}" compose --project-directory /opt/grit-wms \
    --env-file /opt/grit-wms/.env -p "$GRIT_PROJECT" \
    -f /opt/grit-wms/compose.yaml -f /opt/grit-wms/compose.ops.yaml "$@"
}
grit_ops_compose config --quiet
grit_dc build app backup
grit_ops_compose build ops
```

**Не выполнять pull/up db.** PostgreSQL minor/major update не входит в этот этап.
Проверить планируемый bind path, не печатая секреты resolved config:

```bash
python3 - <<'PY'
import json, os, subprocess
project=open(os.path.join(os.environ['GRIT_STATE'],'project')).read().strip()
d=json.loads(subprocess.check_output(['docker','--host=unix:///var/run/docker.sock','compose',
 '--project-directory','/opt/grit-wms','--env-file','/opt/grit-wms/.env','-p',project,
 '-f','/opt/grit-wms/compose.yaml','config','--format','json'],text=True))
mounts=d['services']['db']['volumes']
assert any(v.get('target')=='/var/lib/postgresql/data' and v.get('source')=='/opt/grit-data/postgres' for v in mounts)
assert not d['services']['db'].get('ports')
print('Planned PG bind and non-public database port verified')
PY
```

GRIT_STATE экспортируется в первом блоке. Не пропускать проверку путей/портов.

## 4. Полный baseline и пробное восстановление, пока app остановлен

```bash
export GRIT_STATE
grit_ops_compose run --rm --no-deps ops export \
  --confirm-read-only-access --bundle /operations/baseline
grit_ops_compose run --rm --no-deps ops verify --bundle /operations/baseline
GRIT_TRIAL_DB="restore_check_update_${GRIT_STAMP}"
grit_ops_compose run --rm --no-deps ops trial-restore \
  --confirm-read-only-access --bundle /operations/baseline --database "$GRIT_TRIAL_DB"
```

Ops подключается только к существующей `db:5432/grit` через внутреннюю сеть,
пароль берёт из сохранённой .env через Compose и корректно экранирует libpq.
Параметры Render/другого хоста отвергаются. Восстановление создаёт НОВУЮ
restore_check_* и никогда не заменяет grit. Любой ненулевой integrity counter
блокирует приёмку. Проверяется содержимое всех public-таблиц, не только 5 сотрудников.

## 5. Пересоздать только app, проверить сохранность

```bash
GIT_COMMIT="$GRIT_APPROVED_COMMIT" grit_dc up -d --no-deps --no-build --force-recreate --wait --wait-timeout 180 app
# Дождаться healthy; ошибки/timeout — STOP, не повторять команды вслепую.
grit_dc exec -T app python deploy/healthcheck.py
# При первом старте initializer может ещё работать: повторить диагностику после
# проверки startup/logs, но не принимать warehouse операции до ready/compare.
```

Ожидание healthy в up не заменяет сравнение данных. Не печатать cookies,
полные ENV или application error bodies, если они могут содержать credentials.

```bash
test "$(grit_dc ps -q db)" = "$GRIT_DB_ID"
grit_ops_compose run --rm --no-deps ops compare-live \
  --confirm-read-only-access --bundle /operations/baseline
```

Все существующие таблицы, их схемы, количество и SHA-256 строк должны совпасть.
Единственное разрешённое новое состояние — точная ПУСТАЯ fbs_scan_requests из
PR93. Если таблица уже существовала, её записи тоже должны совпасть полностью.
Неизвестная новая таблица, непустой новый ledger, изменённая ставка/active/name/
бейдж, schema drift, integrity finding или версия PostgreSQL вызывают отказ.
Это проверка maintenance-режима: до неё не логиниться и не сканировать товары.

## 6. Новая копия и запуск только нового backup

Backup должен быть остановлен на время ручного once, иначе возможен безопасный
отказ `Backup already running`. Новый once не удаляет старые архивы.

```bash
grit_dc run --rm --no-deps --entrypoint sh backup /scripts/backup-once.sh
grit_dc up -d --no-deps --no-build --force-recreate --wait --wait-timeout 180 backup
grit_dc exec -T backup sh /scripts/backup-health.sh
test "$(grit_dc ps -q db)" = "$GRIT_DB_ID"
cmp -s .env "$GRIT_STATE/env.saved"
if test -f "$GRIT_STATE/env.backup.saved"; then cmp -s .env.backup "$GRIT_STATE/env.backup.saved"; fi
```

При включённом mandatory S3/Telegram их неуспех блокирует завершение процедуры.
Не вводить настоящие ключи в тестовый Codex. Изменение настроек S3/KMS/Telegram
или секретов — отдельный согласованный этап; сохранённые env не генерировать
заново. Новые обязательные параметры при необходимости внести ДО freeze с
отдельной проверкой, после чего фиксировать baseline файлов.

Только после всех успешных checks разрешить проверку входа, pending, истории,
FBS/replay/QR, приёмки и статистики на разрешённых тестовых данных. Реальный
МойСклад/API-токены не включаются этой Compose-конфигурацией.

## Остановка/откат

Если шаг проверки не прошёл: остановить только app/backup, сохранить DB, все
новые и старые архивы, состояние и логи. Не восстанавливать dump поверх grit
и не менять владельца PGDATA. PostgreSQL остаётся работать с тем же контейнером.
До приёма новых операций baseline и trial copy — точки восстановления.

Безусловный запуск старого образа НЕ является безопасным откатом: старый init
может сбросить active/rate/target/name seed-сотрудников. Перед возвратом старого
кода нужно отдельное испытание этого образа на restore_check_* и сравнение данных,
либо review исправленного rescue release. Не включать старый pruning backup.
Если данные отличаются, доступность сервиса не обещается: безопасное состояние —
остановленная WMS при сохранённых исходной БД и проверенных копиях до разбора.
После новых warehouse записей нельзя откатывать данные простым восстановлением
baseline: понадобится отдельное разрешение и сверка новых операций.

## Отдельные будущие этапы

HTTPS/DNS, публичный доступ, legacy-бейджи, actual Render export/cutover и остановка
writers Render сюда не входят. Для HTTPS использовать docs/HOSTKEY_HTTPS.md только
после отдельного разрешения; overlay пересоздаёт app и убирает loopback port.
Настройка/реальная проверка Yandex, Telegram и ACME остаются ручной приёмкой.

## Автоматический rehearsal

`python3 deploy/verify_rollout.py` создаёт свои NEW bind paths и проект. Проверяет
сохранение DB container ID, обоих env-файлов, старого архива, disabled сотрудника
со своей ставкой/именем, активной смены и pending; делает dump/restore, запускает
новый app и принимает только точную новую пустую таблицу. Требуются совместно
PR92/93/95 и собранные образы grit-wms:hostkey, grit-wms-backup:hostkey,
grit-migration:ops. Не запускать rehearsal на удалённом Docker context.
