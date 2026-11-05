# Пробная миграция PostgreSQL Render → HOSTKEY

Ни один инструмент не запускается автоматически. Подключение к настоящему Render
требует отдельного разрешения владельца. Сейчас тестируются только искусственные
БД в Docker. DNS, рабочий HOSTKEY и исходная PostgreSQL не меняются.

## Что проверяет инструмент

`deploy/migration/migration_tool.py` использует psycopg и PostgreSQL 17 pg_dump/
pg_restore. Source читается в REPEATABLE READ READ ONLY. `pg_export_snapshot`
передаётся pg_dump; JSON manifest строится из того же snapshot, поэтому текущие
записи источника во время экспорта не рассинхронизируют архив и проверку.

Для каждой public-таблицы сохраняются схема колонок/defaults/nullability,
constraints/indexes, количество строк и SHA-256 полного содержимого. Строки
нормализуются PostgreSQL JSONB и сортируются с COLLATE C; timezone UTC. Это может
потребовать времени и временного диска на большой БД. Manifest не содержит самих
записей. Проверяются версия PostgreSQL и расширения. Отличия сериализации/DDL
между major-версиями требуют ручного разбора: инструмент не скрывает их.

Integrity-проверки выявляют дубли WB, сканы без смены/сотрудника или с неправильным
сотрудником, несвязные деятельности, несколько активных смен/деятельностей,
неразрешённые операции приёмки и несвязные FBS request_id (если таблицы существуют).
При trial-restore ненулевые результаты блокируют заключение о готовности.

Каждый bundle создаётся в НОВОМ каталоге с правами 0700; dump/manifest/checksums
0600. SHA-256 проверяется до создания target DB. Trial restore допускает только
новую `restore_check_*`, без DROP/--clean/перезаписи. Ошибка оставляет отдельную
БД для анализа. После восстановления сравниваются все public-таблицы, а не только
пять сотрудников. Архивы и данные автоматически не удаляются.

Ограничения: read-only export не означает, что приложение перестало писать.
Финальный перенос требует реальной остановки всех writers. Состояние sequence
и объектов вне public требует отдельной проверки, если они появятся в проекте.
В snapshot-сравнение входят public-таблицы и расширения, не произвольные функции,
роли, ACL или внешние схемы. Восстанавливать можно только доверенные свои архивы;
checksums защищают целостность, не удостоверяют автора. Храните копию checksums
отдельно. Не подключайте целевую базу к двум независимо работающим WMS.

## Сборка и синтетические тесты

```
docker build -f deploy/migration/Dockerfile -t grit-migration:test .
docker build -t grit-wms:migration-trial .
python deploy/verify_migration_postgres.py
```

Runner сам создаёт временную PostgreSQL в tmpfs и не принимает production DSN.
Для сборки в управляемом TLS proxy передаётся временный build secret proxy_ca;
TLS verification не отключать.

## Экспорт (ТОЛЬКО после отдельного разрешения на чтение источника)

В защищённом файле вне Git задайте GRIT_MIGRATION_DSN; URL/пароль не передавать
аргументом CLI и не печатать .env. Права файла 0600. Для внешнего PostgreSQL
обязателен `sslmode=verify-full` с доверенным CA и правильным hostname;
`prefer`/`require` без проверки имени сертификата отвергаются. CA-файл при
необходимости монтируется отдельно read-only и указывается через sslrootcert. Пользователь должен иметь
read-only доступ ко всем нужным данным; новые учётные записи этим PR не создаются.
Не применять к источнику команды CREATE/ALTER/GRANT/REVOKE.

```
# /secure/bundles — заранее подготовленный защищённый каталог.
# Разрешение --confirm-read-only-access подтверждает только разрешённое чтение.
docker run --rm --env-file /secure/source-connection.env \
  -v /secure/bundles:/bundles grit-migration:test export \
  --confirm-read-only-access --bundle /bundles/trial-001
# Проверка архива БЕЗ подключения к БД:
docker run --rm --network none -v /secure/bundles:/bundles:ro \
  grit-migration:test verify --bundle /bundles/trial-001
```

Это шаблон команд; /secure пути нужно подготовить отдельно. Никаких настоящих
подключений при разработке не выполнялось. Не храните archive/env в GitHub.
Версия клиента должна быть не старее source; PostgreSQL 18 потребует клиента 18
и отдельного подтверждения совместимости, а не принудительного восстановления
в PostgreSQL 17.

## Отдельный trial project на HOSTKEY (после review)

Не используйте обычный compose.yaml с другим project name и тем же bind-путём
PostgreSQL: два контейнера на `/opt/grit-data/postgres` опасны. Используйте только
standalone `deploy/migration/compose.trial.yaml`: у него отдельный named volume,
нет публичного порта базы, приложение по умолчанию НЕ запускается.

Создайте отдельный тестовый env; существующую `.env` не трогать:

```
python3 -c "from pathlib import Path; from deploy.init_env import generate; generate(Path('.env.migration-trial'))"
# В .env.migration-trial добавить только путь к каталогу проверенных bundle:
# GRIT_MIGRATION_BUNDLES=/secure/bundles

docker compose --env-file .env.migration-trial -f deploy/migration/compose.trial.yaml build
docker compose --env-file .env.migration-trial -f deploy/migration/compose.trial.yaml up -d db
docker compose --env-file .env.migration-trial -f deploy/migration/compose.trial.yaml \
  run --rm migration trial-restore --confirm-read-only-access \
  --bundle /bundles/trial-001 --database restore_check_trial
```

После точного сравнения данных можно отдельно разрешить старт тестовой WMS:

```
docker compose --env-file .env.migration-trial -f deploy/migration/compose.trial.yaml \
  --profile serve up -d app
curl --fail http://127.0.0.1:18000/api/startup
# Должно быть ready=true. Проверить также health контейнера и страницу /login.
```

Trial entrypoint допускает только `db/restore_check_*`, удаляет внешние API/S3/
print credentials и запрещает SQLite. Внутренняя сеть не имеет внешнего маршрута.
Приложение при старте выполняет свою штатную инициализацию на trial-копии:
сначала сравните архив ДО старта, после старта отдельно проверьте изменения
схемы/ключей бейджей и количество/содержимое критических операций. Используются
тестовые ключи подписи; trial не должен подтверждать рабочие бейджи или сессии.
До production объединить проверенные исправления сохранения сотрудников/FBS.

Остановка trial не требует удаления volume или каких-либо каталогов. Не выполняйте
`down -v` и не используйте общий source/target каталог. Рабочий compose проект и
его база не участвуют в этих командах.

## План окончательного переключения — без исполнения

1. Зафиксировать версии приложения, PostgreSQL, расширения, секреты подписей,
   исходные параметры и проверенный backup. Провести trial и документировать все
   нулевые integrity-проверки/исключения. Разобрать processing/uncertain приёмки.
2. Отдельно согласовать окно простоя. Остановить на Render ВСЕ процессы записи,
   cron/worker, интеграции и автодеплой. Убедиться в отсутствии активных запросов;
   зафиксировать время/ответственного. DNS сам по себе не останавливает writers.
3. HOSTKEY writer также должен оставаться остановленным. Снять финальный bundle
   после freeze. Повторно проверить контрольные суммы и восстановить НОВУЮ БД.
4. Сравнить схему, строки/содержимое и integrity. Не запускать app при расхождениях.
   Старую Render-базу сохранить. Секреты подписей сохранить для production только
   после разрешения, не использовать их в trial.
5. Отдельным изменением production-конфигурации выбрать единственную целевую БД.
   Текущий стандартный entrypoint жёстко использует `db/grit`; trial-инструмент
   его НЕ переключает и НЕ переименовывает базы. Нужны review и разрешение на
   реальное переключение, HTTPS/DNS и подключение интеграций.
6. Проверить ready=true, БД, вход, историю, pending и read-only интеграционные
   запросы. До первых новых записей можно вернуться к Render без переноса данных.
7. После появления новых записей HOSTKEY простой возврат DNS теряет изменения.
   Для отката снова остановить writers и отдельно согласовать обратный перенос
   с проверкой. Никогда не включать обе расходящиеся базы на запись.

Инструмент намеренно не утверждает, что writers остановлены: это внешнее действие
владельца, которое нельзя доказать одним файлом-флагом или открытым портом.
