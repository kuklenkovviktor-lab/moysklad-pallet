> Дополнение P0 от 9 ноября: [общий deadline, лимиты этапов и безопасная остановка](CHECK_SOURCE_DEADLINES.md). Приведённые ниже прежние лимиты дополнены новым независимым watchdog.

# Render export: нулевой partial и безопасный check-source

Подготовлено 8 ноября 2026. База: `main` / `d0e82d2`, PR98 с read-only системными CA уже включён. Изменения: `fix/render-export-diagnostics`.

## Что установлено, а что нет

**Причина именно рабочего сбоя Render пока не установлена:** к Render и HOSTKEY в этой работе не подключались. `database.dump.partial` размером 0 и прежний код `URL_OR_SCHEMA_OR_ARCHIVE` не доказывают ни проблему URL, ни конкретную ошибку схемы.

Подтверждён дефект диагностики: `pg_command()` перехватывал любую ошибку процесса (`pg_dump`, `pg_restore`, timeout, OSError) и превращал её в общий `Refused`. Worker затем превращал все `Refused` в `URL_OR_SCHEMA_OR_ARCHIVE`, теряя этап и первичную категорию. Нулевой файл создаёт сам pg_dump до завершения экспорта; его существование не доказывает успешное соединение этого процесса.

**На настоящем изолированном PostgreSQL16 воспроизведён идентичный симптом:** роли разрешён snapshot, но запрещён SELECT одной таблицы. Экспорт оставляет `database.dump.partial` ровно 0 байт. Теперь результат — `PG_DUMP:PERMISSION`; `check-source` обнаруживает этот отказ раньше как `READ_ACCESS:PERMISSION`, без создания дампа. Это подтверждённый возможный механизм, **не заключение о правах роли на реальном Render**.

Немодифицированный экспорт `main` на синтетической PG16 также успешно выполнился с достаточными правами. Универсальная несовместимость PostgreSQL16 с клиентом17 не воспроизведена.

Отдельно подтверждён риск наследования `PG*`: раньше subprocess копировал их все. Например, `PGHOSTADDR`, `PGSERVICE`, `PGPASSFILE`, дополнительные SSL-переменные могли дополнять явно разобранный DSN. Теперь psycopg получает окружение без унаследованных PG*, после connect оно восстанавливается; native tools получают новое окружение только с PG-параметрами из разобранного DSN. Проверено на стенде с заведомо неверными PGHOSTADDR/PGSERVICE/PGSSLMODE: preflight проходит по правильному DSN. Воздействие таких переменных на рабочий HOSTKEY неизвестно.

OpenSSL отдельно не доказывает работу psycopg и native pg_dump с теми же trust settings. В проверенном облачном image psycopg использовал libpq **18.6**, native pg_dump/pg_restore/psql — **17.11** (Debian). Версии конкретного HOSTKEY надо проверять новым preflight; они могут отличаться. Dockerfile остаётся `postgres:17-bookworm` + существующий Python/psycopg stage; новых зависимостей нет.

## Что изменено

- Скрытый URL передаётся только JSON/stdin. В argv и Docker Config.Env URL/пароль не записываются. Native subprocess получает пароль через частное окружение процесса, как ранее: администратор ОС может читать окружение процессов, поэтому машина должна быть доверенной.
- Сохранены `sslmode=verify-full`, проверка hostname, явный CA bundle `/etc/ssl/certs/ca-certificates.crt`, read-only mount `/etc/ssl/certs`. Unix-socket URL не принимается за внешний TLS endpoint.
- У каждого контролируемого этапа фиксированные stage/category. SQLSTATE и OS errno используются напрямую; для libpq без SQLSTATE распознаются конкретные первичные verifier/authentication-диагностики, для native clients — известные первичные сообщения. Простые слова `ssl`/`password` в исключении больше не используются как диагноз.
- stdout/stderr native tools остаются в памяти, не печатаются. В консоль поступают только разрешённые коды и числовой успешный результат. Непонятный exit получает `PROCESS_EXIT`, непонятное исключение — `UNKNOWN`; неизвестная причина не превращается в «ошибку TLS» или «недостаток прав».
- `pg_dump --no-password` не уходит в интерактивный запрос. Параметры SSL, credentials, options и database передаются через одну функцию построения libpq environment. Locale клиента C фиксирует форму известных диагностик, не меняя данные/locale источника.
- Пустой или не-PGDMP результат даже после exit 0 не считается экспортом. TOC проверяется отдельно. Ошибки mkdir/read/fsync/finalize имеют свои этапы. Имеющиеся архивы и partial не удаляются и не перезаписываются.
- `check-source` не запускает pg_dump и не создаёт даже schema-only dump. Он запускает `--version` для всех трёх tools, psycopg read-only snapshot, metadata/permissions probes (SELECT LIMIT0, ACCESS SHARE, чтение состояния последовательностей), native psql с тем же DSN/environment, затем импорт того же snapshot. psql связан с системной libpq того же пакета PostgreSQL, что pg_dump. На psql выставлены `-X`, `--no-password`, ON_ERROR_STOP и VERBOSITY=sqlstate. Архивный каталог этому контейнеру не монтируется.
- Preflight берёт кратковременные ACCESS SHARE locks (совместимы с обычными INSERT/UPDATE/DELETE), не пишет схему/данные. На проверках чтения lock_timeout=5s, statement_timeout=60s; native probes ограничены 30s, соединение — 10s. Это не полный row-hash audit и не гарантия чтения всех данных/успешной записи архива.

## Безопасные следующие команды владельцу HOSTKEY

Codex эти команды на HOSTKEY **не выполнял**. После ревью PR получить точный одобренный commit и обновить только код обычным согласованным способом. Не менять `.env`, пароли БД, production Compose или работающие контейнеры. До checkout сохранить локальные изменения, если они существуют; не использовать reset/clean.

В существующем `/opt/grit-wms`:

`python3 deploy/migrate.py build`

Собирает только migration image, app/db/backup не перезапускает. Пересборка обязательна: новый launcher со старым image не даст новые проверки.

`python3 deploy/migrate.py check-source`

В скрытый вопрос вставить одну строку External Database URL Render без кавычек/команд shell. Не вводить URL в командную строку. Успех выводит server_version_num (например, 160010 означает 16.10), major tools и число проверенных таблиц. Пароль, host, username, имена таблиц и строки сотрудников не выводятся. Preflight создаёт только временный Docker-контейнер, не bundle/дамп.

При STOP **остановиться и сообщить только stage/category**, например `READ_ACCESS:PERMISSION`. Не присылать URL, полный DSN, env, Docker inspect/config или сырой traceback. Не выдавать гранты автоматически и не менять рабочую схему ради прохождения проверки: права/владение надо рассмотреть отдельно с владельцем БД.

После PASS и отдельного решения владельца о новом read-only экспорте, проверки свободного места и согласованного окна:

`python3 deploy/migrate.py export`

Выбрать **новый** каталог, например `/opt/grit-data/migration/render-20261108-v4`. Существующий `render-20261108-v3/database.dump.partial` оставить как есть. PASS preflight не гарантирует PASS pg_dump: неполные права на другие объекты, ошибки полного чтения, concurrent DDL, сеть и место на диске всё ещё могут проявиться позже. Финальный дамп для переключения делать после остановки всех writers по основной инструкции миграции.

`python3 deploy/migrate.py verify`

Указать новый успешно созданный bundle: проверяются обе SHA-256 и pg_restore TOC. Нулевой/неполный архив не восстанавливать. Trial restore — только в новую restore_check БД по [основной инструкции](HOSTKEY_FINAL_MIGRATION.md), без DROP/clean и без затрагивания существующих баз.

## Этапы и категории

| Stage | Что выполнялось | Примеры достоверного результата |
|---|---|---|
| INPUT | Разбор JSON/URL/действия | REFUSED или UNKNOWN, без исходной строки |
| TOOLS | pg_dump/pg_restore/psql --version, совместимый major | TOOL_MISSING, VERSION, TIMEOUT |
| CONNECT | Реальный psycopg/libpq connect | TLS по verifier error, AUTHENTICATION по SQLSTATE/конкретному FATAL; иначе CONNECTION/UNKNOWN |
| SNAPSHOT | BEGIN READ ONLY и pg_export_snapshot | PERMISSION по SQLSTATE42501; при иной ошибке её структурная категория или UNKNOWN |
| READ_ACCESS | Metadata, необходимые таблицы, SELECT LIMIT0/LOCK, sequences | PERMISSION, SCHEMA, UNKNOWN |
| NATIVE_CONNECT | Реальное psql/libpq соединение с теми же CA/DSN | TLS/AUTHENTICATION/PERMISSION только при соответствующем первичном сигнале; иначе PROCESS_EXIT |
| SNAPSHOT_IMPORT | Native read-only transaction с тем же snapshot | Код SQLSTATE или PROCESS_EXIT/UNKNOWN |
| PG_DUMP | pg_dump process и запись partial | PERMISSION, TLS, DISK_FULL по errno/известному write diagnostic; иначе PROCESS_EXIT/TIMEOUT |
| MANIFEST | Полное чтение и проверка GRIT/schema/sequence state | PERMISSION, SCHEMA, REFUSED, UNKNOWN |
| ARCHIVE_WRITE | Создание каталога, stat/read/fsync | DESTINATION_EXISTS, PERMISSION, DISK_FULL, IO |
| ARCHIVE_CHECK | Непустой PGDMP, pg_restore --list, необходимые TOC entries | EMPTY_ARCHIVE, INVALID_ARCHIVE, PROCESS_EXIT |
| FINALIZE | Manifest, rename, SHA-256 files | DISK_FULL, PERMISSION, IO |

SOURCE_CHECK/EXPORT используются как внешние границы для неожиданной ошибки вне более узкого контролируемого этапа. Это нейтральные этапы, не утверждение о первопричине. Старые коды поддерживаются launcher для совместимости, но старый image необходимо пересобрать.

## Фактическое тестирование в Codex

- Baseline `main` до исправления: реальный синтетический PG16 TLS export → PG17 restore/app — PASS. Следовательно, неисправность не воспроизводится при любых настройках/любой роли.
- Полный pytest: **342 passed, 16 skipped, 6 существующих warnings**. 16 opt-in PostgreSQL тестов отдельно выполнены runner ниже, не засчитаны по skip как успешные.
- Новые diagnostics unit tests: **12 passed**. SQLSTATE, verifier/auth primary diagnostics, neutral unknown, PG env isolation/DSN→native SSL mapping, snapshot denial, нулевой partial после pg_dump failure, exit0 с пустым/повреждённым output, ENOSPC (fault injection), missing tool, безопасный worker/launcher output, отсутствие bundle mount у check-source. Ошибки с искусственным секретом не выводят его.
- `verify_migration_16_17.py`: **PASS**. Настоящие PG16 TLS issuer/hostname failures, неверный пароль, snapshot EXECUTE denial, SELECT/pg_dump denial с оставленным нулевым partial, PASS preflight без архива, игнорирование отравленных PG*, затем успешный полный PG16 export и PG17 trial restore, hashes/constraints/sequence nextval=51/9, startup готовность и сохранность записей. Ошибки не содержат пароль или URI.
- `verify_migration_postgres.py`: **16/16 passed** и полный GRIT export/restore/startup comparison — PASS. Данные/архивы искусственные, базы создавались только в новом локальном tmpfs Docker.
- Node FBS outbox — PASS; replenishment focus — 8 checks PASS.
- Migration Docker build, Python compile и git diff --check — PASS. Production Compose, складские модули и зависимости не менялись. Новый набор diagnostics добавлен в migration CI; удалённый GitHub Actions не заявляется проверенным.

Логи облачной проверки: `/tmp/grit-diag-baseline.log`, `/tmp/grit-diag-build.log`, `/tmp/grit-diag-pytest.log`, `/tmp/grit-diag-pg16-17.log`, `/tmp/grit-diag-pg17.log`. Успешный последний синтетический PG16 bundle: `/tmp/grit-pg16-17-8a4u7_9_`, PG17 pipeline: `/tmp/grit-migration-bundles-gbfxjgiz`. Это не рабочие данные Render/HOSTKEY.

## Оставшиеся гипотезы

На рабочем HOSTKEY не проверены фактические SELECT/sequence/schema/object privileges роли, доступ к snapshot, реальное окружение PG*, отличие native-client TLS от OpenSSL, конкретная схема/расширения и место/права записи в каталог. Сеть может изменяться между соединениями. Предварительные подтверждения владельца о TLS сохранены как исходная информация, но не заменяют native checks. Ни одна из этих гипотез не объявлена доказанной причиной рабочего сбоя. Следующий безопасный шаг — `check-source`, затем при необходимости новый экспорт с кодом точного этапа.
