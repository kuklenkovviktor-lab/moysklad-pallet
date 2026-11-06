# Проверка финального migration kit — 2026-11-06

База изменений: `origin/main` — `abb763ab762770b8a617841e816b810cd6aaf3f0` (PR92–96 объединены). Ветка: `feat/render-hostkey-migration-kit`. Рабочие Render, HOSTKEY, DNS, реальные PostgreSQL, S3, Telegram и API-токены не использовались. Проверки выполнялись в Codex на локальном Docker socket, с искусственными данными и временными каталогами. Изменения в production не развёртывались.

Основной документ: [HOSTKEY_FINAL_MIGRATION.md](HOSTKEY_FINAL_MIGRATION.md) — содержит реестр данных, секретов, короткие команды, контроль каждого этапа, остановку и откат.

## Подтверждённые ограничения и исправления

1. Manifest прежнего экспорта не проверял последовательности/identity и locale. Теперь проверяются их параметры и состояние, полные типы столбцов (включая typmod/collation/generated/default), все строки/ограничения/индексы, представления, функции, триггеры, enum и extensions. Восстановление создаёт новую БД с исходными encoding/libc locale. Дополнительные непроверяемые хранилища, RLS, SECURITY DEFINER и non-libc locale блокируют автоматическое принятие.
2. Внешний URL ранее надо было самостоятельно передавать через окружение. Новый интерактивный экспорт скрывает ввод, требует терминал, очищает оставшиеся вставленные строки на POSIX, отказывает multiline/командам Bash, передаёт URL только stdin. Источник — verified TLS + read-only transaction и согласованный snapshot. В Git/argv/Docker Config.Env URL не сохраняется. Ошибки классифицируются без раскрытия libpq-сообщений.
3. PostgreSQL dump не содержит оригинальные PDF/XLSX, локальные журналы пилотной печати, секреты и данные браузера. Реестр указывает конкретные таблицы/исходники и границы сохранения. Добавлена упаковка оригиналов с пофайловой SHA-256 и проверкой содержимого tar без распаковки; отдельная внешняя S3-копия этих файлов.
4. Штатные app/healthcheck/backup были привязаны к `grit`, токены интеграций отключены. Дополнительные overlays выбирают новую `restore_check_*` БД согласованно для app/healthcheck/backup, сохраняя существующую `grit`. Токены подключаются только явным live overlay после cutover-проверок. Базовый тестовый Compose сохраняет прежнее поведение.
5. Свежая копия тестовой `grit` могла отложить первый backup восстановленной БД из-за общей `.last-local.json`. Финальный запуск принудительно проверяет уведомления, создаёт/загружает свежую копию выбранной БД и требует backup-health до включения интеграций.
6. Локальный ONIX print agent был жёстко привязан к Render. Добавлен явный `--server https://gritwms.space`, сохранился прежний default, произвольные серверы запрещены. Тесты не печатают физические этикетки.
7. Добавлены отказ перезаписывать bundles, запись dump через partial, проверка структуры/размера/хэшей, запрет размещать миграционные данные в Git checkout, игнорирование partial/tar.gz. Экспортёр работает под UID/GID оператора, инструменты в image доступны для чтения: Docker не оставляет недоступный оператору root-owned bundle при непривилегированном запуске.
8. Новый update проверяет существующий Compose/db/bind path, сохраняет приватные конфиги и baseline, пересоздаёт только app/backup и проверяет сохранение записей, secret-file hashes и db container ID. После выбора восстановленной/live БД тестовый updater отказывает, чтобы не вернуть приложение случайно к `grit`.
9. Сбой запуска целевых сервисов останавливает целевые writers. Отдельный stop определяет свой Compose project по контейнеру db и labels, не требует исправной `.env`/ACME-конфигурации и не останавливает db.
10. Внешняя копия переносит все три файла bundle, использует уникальный prefix, SSE, HEAD + условный CompleteMultipartUpload, полное GET SHA-256 и receipt только после проверки всех файлов. Подтверждено различие unversioned `VersionId="null"` и настоящей версии; literal null не передаётся как VersionId в HEAD/GET.

## Фактически выполненные проверки

| Проверка | Результат | Граница проверки |
|---|---|---|
| Полный Python pytest | **329 passed, 16 skipped, 6 warnings** | 16 opt-in PostgreSQL тестов запускаются отдельным runner ниже. Предупреждения Swig/PytestReturnNotNoneWarning существующего теста TSPL, не скрыты |
| `deploy/verify_migration_postgres.py` | **16/16 passed**, полный GRIT export/restore/app readiness/после-startup сверка — PASS | Свежий PostgreSQL17 tmpfs; read-only запрет записи, неизменность источника, существующие target/bundle, повреждённая checksum, schema/row/sequence drift, integrity, extra schema/large objects, upgrade gate |
| `deploy/verify_migration_16_17.py` | **PASS** | Настоящий PG16 с локальным TLS-сертификатом → stdin URI export клиентом17 → отдельный PG17. Unicode, row SHA, constraints/indexes, identity/view/sequence. `nextval` после переноса — 51 и 9; повторный export отклонён; восстановленный GRIT Healthy, бизнес-записи совпали |
| `deploy/verify_final_migration.py` | **PASS** | Вызван реальный update/trial-app/stop на отдельной копии кода и новых bind directories. Прежние db ID, записи/отключённый сотрудник/ставка/цель, `.env`/`.env.backup` и старый архив сохранены; `grit` и новая restore_check существуют независимо, интеграции пустые, выбранная БД backup правильная, повторный тестовый update после выбора restore_check отклонён |
| `deploy/verify_stack.py --s3` | **PASS** | Moto5.1.17 через HTTPS с проверяемым CA, обычный backup/download/restore, полная миграционная копия трёх файлов, пофайловый SHA после GET; versioned/unversioned режимы; отказ существующему объекту; отдельная копия tar оригиналов; фактический disconnect/reconnect S3-сети, сохранение очереди и recovery без лишнего dump; пустая схема/повреждённый архив/существующая target отклонены; данные после restart/recreate сохранены |
| `deploy/verify_fbs_postgres.py` | **14/14 passed** | Конкурентные сканы, повтор/потерянный ответ, pending/shift persistence, QR skip и завершение — изолированный PG17 |
| `deploy/verify_receiving_postgres.py` | **4/4 passed** | Конкурентная приёмка/количество, блокировки и освобождение после внешней ошибки — PG17 с имитацией API |
| `deploy/verify_https.py` | **PASS** | Частный CA/изолированный Caddy: TLS, redirect, Secure cookie, camera permission, headers, служебные/admin ограничения, spoofed XFF. Не реальный ACME |
| Новые safety-тесты | **17/17 passed**, входят в общий pytest | stdin/secret/URI, dotenv parser настоящего Compose (спецсимволы не подменяются), file archive corruption/overwrite/symlink, external receipt, остановка при сбое live, отказ тестовому updater на live, backup/notify/health перед app, полный merge restore+live+HTTPS без порта db/app |
| Node FBS outbox | **PASS** | Lost ACK, reload/replay, сохранение после server failure, definitive rejection, отказ localStorage до отправки |
| Node replenishment focus | **8 checks PASS** | HID scanner focus transitions |
| Hidden input через настоящий PTY | **PASS** | Искусственная строка не отражена в терминале, выведена только длина; внешних подключений не было |
| Docker app/backup/migration images | **Build PASS** | Официальные базовые образы, CA proxy передан BuildKit secret, TLS не отключался |
| `git diff --check`, Python compile, ignore-фильтры | **PASS** | `.env.cutover`, dump.partial, tar.gz игнорируются; новых секретов в tracked-файлах нет |

Первый параллельный Docker-прогон исчерпал место в облачной среде (`No space left on device`), не был принят за успешный. После завершения/уборки только собственных временных контейнеров соответствующие PostgreSQL16/17, update, S3, receiving и HTTPS проверки выполнены последовательно и прошли. Синтетические bind-каталоги/архивы сохранены; рабочие данные не удалялись. В S3-smoke при намеренной аварии печатается сообщение об отсутствии настоящего Telegram — реальных реквизитов у стенда нет; ошибка ожидаема, recovery проверен.

Тексты логов находятся в `/tmp/grit-kit-*.log` облачной сессии; это временные артефакты, не рабочие реквизиты. Успешные bundles/стенды: `/tmp/grit-pg16-17-247s2cek`, `/tmp/grit-migration-bundles-vk2tjcpz`, `/tmp/grit-final-kit-vwy1kx09`, `/tmp/grit-verify-_0jzy_aw`, TLS — `/tmp/grit-https-xznyczng`.

## Изменённые файлы

- `deploy/migrate.py` — основной интерактивный launcher и проверка/упаковка внешних файлов.
- `deploy/migration/migration_tool.py`, `input_entrypoint.py`, `copy_bundle_s3.py`, `Dockerfile` — source read-only export, полная сверка, новая target, S3 protocol.
- `compose.restore.yaml`, `compose.live.yaml`, `deploy/entrypoint.py`, `deploy/healthcheck.py`, `deploy/backup.Dockerfile` — выбор локальной восстановленной БД, интеграции и согласованные проверки/backup.
- `onix_print_agent.py` — явный выбор HOSTKEY для локального агента.
- `deploy/verify_migration_16_17.py`, `verify_final_migration.py`, `verify_stack.py`, `tests/test_migration_postgres.py`, `tests/deployment/test_final_migration.py`, `test_rollout_tools.py`, `.github/workflows/postgres-migration.yml` — автоматические проверки и CI.
- `.gitignore`, `.dockerignore` — защита миграционных артефактов.
- `docs/HOSTKEY_FINAL_MIGRATION.md`, `FINAL_MIGRATION_TEST_REPORT.md`, `HOSTKEY_DEPLOYMENT.md` — единая инструкция, реестр и отчёт.

## Что остаётся владельцу и что блокирует переключение

Код и сценарий готовы к ревью и репетиции. Фактическое production-переключение **не разрешено результатами синтетических тестов**: сначала проверить комплектность Render Environment/Secret Files/disk/операторских оригиналов, действующие signing keys, отсутствие внешних/фоновых writers, реальную schema/locale/extensions и integrity. Неизвестная схема или несверенные receiving confirmations требуют остановки.

Отдельные обязательные проверки: внешний сетевой/TLS доступ к Render URI, состояние/commit/mounts/свободное место HOSTKEY, реальный Yandex endpoint/bucket/права/поддержка условного multipart и независимое скачивание, Telegram, DNS/AAAA/ACME/firewall/CIDR, камера и реальная печать, соответствие документов МойСклад. Не менять рабочую Render БД для прохождения проверки. Не использовать финальную БД для репетиционных бизнес-записей. До финального экспорта вручную остановить всех Render writers; после первых HOSTKEY записей простой возврат DNS без reconciliation запрещён.

GitHub API в этой среде возвращает `Forbidden`; результаты новых GitHub Actions здесь не проверены. Локальные результаты выше — фактически запущенные проверки, не утверждение о статусе удалённого CI. Ветка предназначена для отдельного PR, автоматическое объединение/деплой не выполняются.
