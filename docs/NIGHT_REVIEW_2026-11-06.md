# GRIT WMS — проверенные результаты ночного цикла, 6 ноября 2026

## Границы работы

Работа выполнена в облачной Linux-среде Codex. Рабочие Render, HOSTKEY, DNS,
производственные PostgreSQL/S3, API-токены и SSH-доступ не изменялись. Рабочие данные
и существующие архивы не удалялись; удалялись только созданные runner-ами
контейнеры с искусственными данными. PostgreSQL-тесты использовали новые
Docker-базы с искусственными данными; S3 — HTTPS Moto с тестовым CA; TLS —
внутренний CA в отдельной сети. Ни один PR не объединялся, main не изменялся.

База разработки — origin/main ee47d14. Для проверки совместимости создана
НЕОПУБЛИКОВАННАЯ локальная test/night-cycle-integration с копиями коммитов
независимых веток. Это не объединение PR и не развёртывание.

## Подтверждённые ошибки и исправления

| Область | До изменения | Проверенное исправление |
|---|---|---|
| PR №92: конфигурация | Опечатка boolean молча отключала S3 | Некорректное значение отклоняется |
| PR №92: очередь | Один плохой архив блокировал загрузку всех следующих | Проверяются все pending; общий статус ошибочный до устранения ошибок |
| PR №92: Telegram | Неудавшееся уведомление терялось, если backup успел восстановиться | Устойчивая очередь уведомлений, повторы даже после recovery |
| FBS | Скан, pending и событие фиксировались разными commit | Одна транзакция и блокировка смены |
| FBS lost ACK | После server commit клиент не мог безопасно подтвердить результат | Persisted request_id + браузерный outbox, повтор без второго учёта |
| FBS concurrency | Два разных WB могли пройти предварительную проверку одного pending | Повторная проверка под блокировкой; один победитель |
| QR-пропуск | Событие записывалось после commit токена/pending | Токен, pending и событие в одной транзакции; совместная блокировка со сканом |
| Смены | Создание/переключение состояло из нескольких commit | Атомарные записи и сериализация по сотруднику/смене |
| Импорт PDF | Конкурентный конфликт мог скрываться ON CONFLICT DO NOTHING | Проверяется фактически победившее сопоставление |
| Уникальность WB | Ошибка обязательного уникального индекса скрывалась | Приложение отказывает в готовности без защиты; данные не исправляются автоматически |
| Сотрудники | Startup включал отключённых seed-сотрудников и сбрасывал настройки | Существующие active/rate/target/name/role сохраняются |
| Приёмка МойСклад | Две палеты читали 10 и обе записывали 15 | Per-document lock; подтверждён итог 20 при двух прибавлениях по 5 |
| Проведение документа | Сводка/проведение не разделяли блокировку с прибавлением | Проверка и проведение ждут тот же документ |

Существующая уникальность WB и одноразовость QR не объявлялись отсутствующими:
они уже были в проекте. Исправлены подтверждённые границы транзакций/гонки.

## Ветки и основные файлы

| Ветка | Содержание и файлы | Review |
|---|---|---|
| fix/hostkey-s3-backups | deploy/backup_worker.py, deploy/verify_stack.py, tests/deployment/test_backup_worker.py, docs/PR92_REVIEW.md | Обновлён существующий [PR №92](https://github.com/kuklenkovviktor-lab/moysklad-pallet/pull/92) |
| fix/fbs-durable-transactions | wms.py, fbs_transactions.py, fbs_skip_authorization.py, pdf_fbs_registry.py, tests/test_fbs_durable_transactions.py, tests/test_fbs_outbox.cjs, deploy/verify_fbs_postgres.py, CI | [Создать PR](https://github.com/kuklenkovviktor-lab/moysklad-pallet/compare/main...fix/fbs-durable-transactions?expand=1) |
| fix/receiving-document-lock | app.py, receiving_lock.py, tests/test_receiving_concurrency.py, deploy/verify_receiving_postgres.py, CI | [Создать PR](https://github.com/kuklenkovviktor-lab/moysklad-pallet/compare/main...fix/receiving-document-lock?expand=1) |
| feat/postgres-migration-preflight | deploy/migration/*, deploy/verify_migration_postgres.py, tests/test_migration_postgres.py, CI | [Создать PR](https://github.com/kuklenkovviktor-lab/moysklad-pallet/compare/main...feat/postgres-migration-preflight?expand=1) |
| feat/hostkey-https-proxy | compose.https.yaml, deploy/https/Caddyfile, deploy/verify_https.py, docs/HOSTKEY_HTTPS.md, CI | [Создать PR](https://github.com/kuklenkovviktor-lab/moysklad-pallet/compare/main...feat/hostkey-https-proxy?expand=1) |

GitHub API/GraphQL отвечает Forbidden. Поэтому завершение удалённого CI, причины
возможных удалённых ошибок и создание новых PR через API НЕ подтверждены.
Доступ Git работает: ветки опубликованы, refs/pull/92/head после обновления
совпадает с d2ca4c6. Владелец должен проверить [checks PR №92](https://github.com/kuklenkovviktor-lab/moysklad-pallet/pull/92/checks).
Локальная зелёная проверка не заменяет удалённый CI и разрешение на merge.

## Фактически выполненные проверки

| Проверка | Результат |
|---|---|
| pytest всего объединённого кандидата, включая функции в корне | 306 passed, 9 skipped, 6 warnings |
| Отдельная FBS-ветка: полный pytest перед публикацией общего CI | 278 passed, 6 warnings |
| 9 пропущенных миграционных тестов в отдельной PostgreSQL 17 | 9 passed, не оставлены непроверенными |
| FBS транзакции, rollback, race, повтор запросов на PostgreSQL 17 | 14 passed |
| Приёмка/проведение документа на PostgreSQL 17 | 4 passed; МойСклад transport искусственный |
| Backup/deployment unit suites | 29 passed |
| Браузерный outbox в Node VM | Lost ACK после commit, reload/replay, 5xx, 409 и отказ хранилища до отправки — PASS |
| Фокус сканера пополнения | 8 checks PASS |
| Сборки Docker app, backup, migration | PASS |
| Backup → HTTPS S3 → полный GET/SHA-256 → download → PostgreSQL restore | PASS |
| Настоящий сетевой разрыв до S3-эмулятора | Архив/очередь сохранены, health ошибочный, recovery без лишнего dump — PASS |
| Пустая PostgreSQL-схема | Архив не создан — PASS |
| Restart/recreate app/db/backup на изолированном стенде | Данные сохранены — PASS |
| Snapshot/export/restore полной схемы GRIT и запуск восстановленного app | Сравнение содержимого и ready=true — PASS |
| TLS/reverse proxy в изолированной сети | Проверяемый сертификат, redirect, headers, Secure cookie, admin/service deny, защита XFF — PASS |
| Production Caddyfile и отсутствие host ports app/db в HTTPS overlay | PASS |

Warnings pytest: уведомления PyMuPDF/SWIG и случайно собираемый helper
onix_usb_print_test.test_label_tspl, возвращающий bytes. Это не падения; физическая
печать не запускалась. Telegram и МойСклад проверялись через mock transport; HTTPS/S3 — через реальные
локальные протоколы к искусственным стендам. Живые внешние интеграции не вызывались.

Pytest 8.4.2 установлен только в тестовую venv из официального PyPI, чтобы реально
выполнить функции тестов в корне. requirements приложения не менялся.

## Что именно подготовлено для миграции и HTTPS

Миграция: read-only exported snapshot одновременно для pg_dump и fingerprint
всех public-таблиц; schema/columns/defaults/indexes/constraints, количество и
SHA-256 содержимого; SHA-256 bundle; запрет существующей target DB; отдельная
restore_check_*; запрет непроверяемого TLS к внешней PostgreSQL. Standalone trial
Compose использует собственный volume и по умолчанию не запускает app. Trial
entrypoint допускает только локальную restore_check_* и удаляет внешние credentials.
Процедура остановки writers и отката описана в docs/POSTGRES_MIGRATION.md.

HTTPS: Caddy с автоматическим обновлением ACME и постоянным storage сертификатов,
закрытыми служебными маршрутами и allowlist админских IP. Клиентский XFF заменяется;
mobile camera разрешена на same origin. App/db не публикуют host ports. Публичный
сертификат не запрашивался и его продление на настоящем домене не проверялось.

## Оставшиеся ограничения и блокирующие условия

1. Не подтверждён удалённый CI PR №92 и новых веток; review и merge выполняет владелец.
2. Реальные версия/расширения/схема/данные Render не читались. Исторические дубли,
   orphan-записи, несколько активных смен и unresolved receiving нужно проверить
   на разрешённом snapshot. Инструмент их выявляет, но ничего не удаляет/исправляет.
3. Предсказуемые альтернативные форматы входа по бейджу из прежнего аудита остаются
   риском. Решение об отключении legacy-форматов и переиздании бейджей нужно принять
   до публичного production-запуска, сохранив нужный сотрудникам сценарий входа.
4. МойСклад lock координирует только экземпляры, использующие одну PostgreSQL.
   Ручное изменение документа, другая система или второй независимый DB writer
   им не защищены. Нужен единственный writer при cutover.
5. Настоящие Yandex/KMS/Telegram, ACME, DNS, firewall и устройства не принимались.
   Нет доказательства доставки на реальный телефон или работы камеры/печати
   на физическом оборудовании. SSH/права этой облачной среды не исправлялись.
6. Fingerprint охватывает public-таблицы и расширения, не произвольные внешние
   схемы/функции/роли/ACL. При наличии таких объектов нужна дополнительная проверка.

Следовательно, автоматический переход на production HOSTKEY пока НЕ разрешён
и не объявлен безопасным. Подготовлены проверенные изменения и инструменты;
актуальное состояние самого VPS в этом цикле не диагностировалось.

## Действия владельца утром

1. Проверить последние commits PR №92, удалённые checks и создать четыре PR по
   ссылкам. Рассмотреть транзакции FBS/приёмки и совместный локальный отчёт.
2. На изолированном HOSTKEY обновлять только явно согласованный сервис/код;
   не выполнять автоматический deploy всех веток. Для backup есть отдельная
   инструкция docs/HOSTKEY_BACKUP_UPDATE.md. Архивы не удалять.
3. Настроить тестовые Yandex bucket/KMS и Telegram локально в защищённом
   .env.backup; проверить внешний объект, SHA-256 и реальную доставку уведомления.
4. Проверить FBS с телефона/сканера: lost connection/reload, ожидающий товар,
   повтор WB, QR skip, переключение деятельности и историю смен.
5. Разрешить отдельное read-only чтение Render, только затем создать проверенный
   bundle и восстановить его в отдельном trial project. Разобрать integrity findings.
6. До DNS/HTTPS включения решить legacy badge риск, настроить административные CIDR,
   проверить порты, CA/ACME и резервные копии. Дать отдельное разрешение на cutover.
7. Согласовать остановку всех writers Render, финальный export/compare и единственный
   writer HOSTKEY. Сохранить Render для отката; после новых операций простой
   возврат DNS недопустим без обратного переноса изменений.
