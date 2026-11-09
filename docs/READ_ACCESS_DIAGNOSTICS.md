# READ_ACCESS: подэтапы, блокировки и медленные запросы

9 ноября 2026. База: `main` / `4497cdb` (PR100). Ветка `fix/read-access-substages`.

## Вывод аудита

Рабочий результат `READ_ACCESS:TIMEOUT` доказывает превышение бюджета этапа, но не доказывает блокировку определённой таблицы. Внутри прежнего READ_ACCESS выполнялись: установка двух локальных лимитов, пять проверок схемы, получение списка public-таблиц, отдельные LOCK TABLE и SELECT LIMIT0 для каждой таблицы, получение определений последовательностей и чтение их состояния. Общий watchdog30s включал все сетевые round trips.

Подтверждена избыточная операция: `SELECT * ... LIMIT 0` при планировании сам берёт ACCESS SHARE lock и проверяет SELECT всех столбцов. Отдельный `LOCK TABLE ... IN ACCESS SHARE MODE` дублировал захват и запрос к серверу. Он удалён **только из preflight**. При T таблицах и S последовательностях число запросов успешно проходящего READ_ACCESS уменьшено с `9+2T+S` до `9+T+S`. Это подсчёт по коду, не измерение задержек рабочего Render. Влияние RTT, серверной нагрузки или DDL-блокировки на рабочем сервере остаётся гипотезой.

Preflight больше не получает неиспользуемые start/min/max/cache definitions через pg_sequences. Вместо этого читает список public sequences из pg_class/pg_namespace и проверяет доступ к last_value/is_called. Полные определения и состояния по-прежнему проверяются неизменённым экспортом/manifest.

## Безопасные подэтапы

| Код | Что проверяется | Лимит |
|---|---|---|
| RA_SCHEMA | Настройка локальных SQL-лимитов и существующие проверки неподдерживаемых storage/RLS/SECURITY DEFINER | 10s wall-clock на вызов блока, 3s на SQL после установки лимита |
| RA_TABLE_LIST | Получение public-таблиц и наличие обязательных GRIT-таблиц | 5s wall-clock, 3s SQL |
| RA_TABLE_READ | Один SELECT * LIMIT0: implicit ACCESS SHARE + права чтения всех столбцов; без чтения строк | 5s на таблицу, 3s SQL, 1s lock_timeout |
| RA_TABLE_LOCK | Результат доказанного отказа implicit lock при SELECT, SQLSTATE55P03 | Категория LOCK_UNAVAILABLE; отдельный LOCK запрос не выполняется |
| RA_SEQUENCE_LIST | Получение имён public sequences из каталога | 5s wall-clock, 3s SQL |
| RA_SEQUENCE_READ | Чтение last_value/is_called одной последовательности, без nextval/setval | 5s на последовательность, 3s SQL |

READ_ACCESS имеет прежний общий предел30s, worker120s, host150s плюс bounded cleanup. Ни общий READ_ACCESS, ни общий бюджет не сбрасываются на новой таблице/подэтапе. Если родительский бюджет истёк во время RA_TABLE_READ, выводится активный подэтап с TIMEOUT; если между операциями — возможен READ_ACCESS:TIMEOUT. TIMEOUT не объявляется доказательством блокировки.

`LOCK_UNAVAILABLE` возникает только из структурированного PostgreSQL SQLSTATE55P03. Например, ограничитель lock_timeout при ожидании ACCESS EXCLUSIVE держателя. PERMISSION42501 остаётся PERMISSION; неизвестный текст «lock timeout» не превращается в диагноз блокировки. SQL statement timeout сохраняет прежнюю классификацию TIMEOUT только при явном verifier-сообщении psycopg; неоднозначная отмена57014 остаётся CANCELLED. Ошибки не игнорируются: preflight завершается отказом.

В прогресс/ошибки передаются только фиксированные коды. Имена схем/таблиц/последовательностей, строки сотрудников, SQL-тексты и исходные исключения не выводятся. Для диагностики запрещённых объектов не отключаются проверки схемы, TLS или права.

## Что не менялось

Функции `export`, `manifest`, `sequence_state` имеют **тот же AST**, что origin/main. Запросы и политики общей проверки схемы `source_tables` сохранены; добавлены необязательные instrumentation contexts, включаемые только check-source (`preflight=True`). По умолчанию, в экспорте, эти contexts отсутствуют. Архивный формат, snapshot, контрольные суммы, SQL полного экспорта и восстановление не менялись. WMS-модули, production Compose, `.env`, API-токены не изменялись.

## Фактические тесты

- Полный pytest: **360 passed, 16 skipped, 6 существующих warnings**. Opt-in16 PostgreSQL тестов не запускались в этом прогоне и не считаются прошедшими по skip.
- Новые unit/process tests: **6 passed** (входят в360): отсутствие лишнего LOCK/получения sequence definitions, сохранение PERMISSION/UNKNOWN, SQLSTATE55P03, hard deadline каждого подэтапа, общий READ_ACCESS budget при повторных операциях, разделение schema/list событий и безопасный host parser.
- Настоящий PostgreSQL16, отдельная synthetic internal Docker network:
  - Обычный check-source прошёл, включая sequence checks.
  - Другая fixture-сессия удерживала ACCESS EXCLUSIVE на employees. Проверка отказала `RA_TABLE_LOCK:LOCK_UNAVAILABLE`; blocker был завершён ROLLBACK, не изменяя строки.
  - `pg_sleep(10)` вместо соответствующего тестового SQL в четырёх случаях дал `RA_SCHEMA:TIMEOUT`, `RA_TABLE_LIST:TIMEOUT`, `RA_TABLE_READ:TIMEOUT`, `RA_SEQUENCE_READ:TIMEOUT` при SQL-лимите3s; каждый случай завершился <8s.
  - Полный snapshot fingerprint содержимого, схемы и последовательностей до/после совпал. Nextval и записи/DDL в этих проверках не выполнялись.
- Node FBS outbox — PASS; replenishment focus — 8 checks PASS. Python compile и git diff --check — PASS.
- AST-сравнение функций экспорта/manifest/sequence_state и сравнение SQL schema audit с origin/main — PASS.

Из-за заполнения cloud rootFS новый Docker image не собирался. Тесты исполняли **актуальный текст migration_tool.py из ветки через stdin** в ранее созданном synthetic migration container; filesystem контейнера оставался read-only. Это не production механизм запуска. Все зависимости, native PostgreSQL clients и проверка сертификата те же; Dockerfile и зависимости не менялись. Свежая сборка и создание нового fixture включены в CI, но удалённый GitHub Actions здесь не подтверждён. В текущей среде проверен режим повторного использования только UUID-labeled собственного synthetic fixture, не создание нового стенда.

Команда, фактически выполненная в Codex: `python3 deploy/verify_read_access.py --reuse-fixture abb7072c833d45f1a3d90dd4dd89ec11`. Проверяется UUID-label tool, единственная internal network, namespace fixture. Реальные Render/HOSTKEY не подключались. Synthetic source/tool остановлены и сохранены; базы, сеть, архивы и контейнеры не удалялись. Артефакты предыдущего fixture остаются в `/tmp/grit-deadline-7xl7i2he`; логи этого прогона — `/tmp/grit-ra-integration.log`, `/tmp/grit-ra-pytest.log`.

## Следующий шаг владельцу HOSTKEY

После ревью PR и получения одобренного кода в `/opt/grit-wms`:

`python3 deploy/migrate.py build`

`python3 deploy/migrate.py check-source`

Первая команда пересобирает только migration image, не WMS и не PostgreSQL. URL вводить скрыто. Сохранить последние фиксированные подэтапы и итоговый stage/category; URL, .env, данные и raw traceback не присылать.

При RA_TABLE_LOCK:LOCK_UNAVAILABLE не отменять рабочие транзакции и не менять права автоматически: владельцу отдельно проверить запланированные DDL/долгие транзакции в приватной сессии. При TIMEOUT не считать блокировку установленной без SQLSTATE; это может быть SQL, сеть или исчерпанный суммарный бюджет. Существующие partial/bundles оставить без изменений. Реальная причина конкретного READ_ACCESS timeout требует результата новой проверки на HOSTKEY; Codex её не объявляет установленной.
