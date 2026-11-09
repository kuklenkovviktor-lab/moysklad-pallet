# P0: ограничение зависаний check-source

9 ноября 2026. База `main` / `5ec5c22`. Ветка `fix/check-source-deadlines`.

## Подтверждённый результат аудита

Точная причина зависания **на рабочем HOSTKEY не установлена**: Codex к HOSTKEY и Render не подключался. Успешные TCP/TLS/pg_isready и доставка stdin не доказывают завершение аутентификации, SQL snapshot, metadata/locks или native client. Traceback на `subprocess.run()` указывает на ожидание вызывающей стороны, не на конкретную операцию PostgreSQL.

В коде подтверждены три пробела защиты:

1. Host launcher ожидал `docker run` без общего timeout, включая передачу stdin и ожидание Docker.
2. SQL statement_timeout устанавливался только на READ_ACCESS; BEGIN/SET/pg_export_snapshot до этого не были им ограничены. Timeout SQL не защищает от зависания DNS/libpq/socket или завершения соединения. Лимит каждого SQL также не ограничивает сумму операций этапа.
3. `capture_output=True` скрывал текущий этап до завершения команды, поэтому отличить CONNECT от SNAPSHOT или metadata было невозможно.

Теперь зависший этап имеет независимый wall-clock deadline. Исправление ограничивает ожидание и даёт свидетельство о последнем активном этапе; оно **не выдаёт гипотезу о рабочем сбое за установленную причину**.

## Устройство и лимиты

Внутри migration container supervisor запускает проверку в отдельном Linux-процессе. DSN остаётся в private memory/stdin; не попадает в argv/Config.Env. Через внутренний pipe передаются только фиксированные коды этапов, категории и три целочисленных поля успешного результата. `verify-full`, hostname и read-only CA mount сохранены.

| Этап | Wall-clock лимит |
|---|---:|
| TOOLS | 10 с на проверку клиента |
| CONNECT | 15 с |
| SNAPSHOT | 15 с |
| READ_ACCESS | 30 с на весь этап, не на каждую таблицу |
| NATIVE_CONNECT | 20 с |
| SNAPSHOT_IMPORT | 20 с |
| CLOSE | 10 с |
| Весь worker check-source | 120 с после разбора входных данных |
| Host Docker/stdin/ожидание | 150 с после скрытого ввода URL |

В дополнение действуют session-only statement_timeout=15s и lock_timeout=5s, передаваемые при connect, **до первого SQL**; connection timeout10s сохранён. Это параметры только текущего подключения, не ALTER SYSTEM/ALTER DATABASE и не изменение production-конфигурации. Соединение закрывается явно, без ожидания commit/rollback в context manager. Лимиты относятся к **check-source**, а не к длительности полного экспорта/полного row-hash audit.

`at_stage` сообщает supervisor начало/конец этапа. Supervisor отслеживает общий монотонный deadline и пределы вложенных этапов. При превышении останавливается вся process group дочерней проверки, включая native psql/tool descendants: SIGTERM и затем SIGKILL при необходимости, до ~2с на завершение. Это работает и при blocked C/libpq/DNS, где одного Python signal или SQL statement_timeout недостаточно.

Host launcher читает stderr параллельно и выводит только строки вида `check-source: CONNECT`; raw stderr/исключения и данные таблиц не транслируются. BEGIN/END переводят наблюдаемый этап обратно в SOURCE_CHECK между операциями, поэтому завершённый этап не объявляется активным бесконечно. INPUT виден ещё до чтения JSON; CONTAINER_START означает отсутствие более позднего маркера, а не доказанную проблему PostgreSQL.

Host ограничитель охватывает и запись stdin отдельным потоком. Если Docker/transport завис, CLI process group завершается, затем проверяется UUID-label **только вновь созданного** диагностического контейнера и посылается bounded stop. Контейнер сохраняется: `--rm` для check-source больше нет; `rm/down/prune` в пути проверки не вызываются. Остановки по общему имени сервиса/маске контейнеров нет.

Остановка Docker ограничена inspect3s + stop5s + inspect3s; завершение CLI — до2s, короткие joins — до0.6s. При работоспособной ОС верхняя граница host ожидания с cleanup — приблизительно **165 секунд после ввода**, обычно worker отказывает раньше. Это не обещание управления неотвечающим ядром/демоном: если Docker не подтверждает остановку, возвращается `Container stop unconfirmed` с именем только созданного контейнера. У worker остаётся собственный deadline, независимый от CLI.

При Ctrl+C host прекращает ожидание, пытается завершить свой CLI и остановить только собственный контейнер; категория CANCELLED. Посторонние контейнеры, базы, архивы и partial не затрагиваются. Повторное внешнее принудительное прерывание самого процесса во время cleanup может не дать подтвердить остановку; в этом случае владелец проверяет указанный контейнер отдельно.

## Сообщения и границы диагноза

- `check-source: SNAPSHOT` — наблюдаемый активный этап, без URL, имени сотрудника, имени таблицы и SQL-значений.
- `... failed at SNAPSHOT; category TIMEOUT` — действительно истёк локальный wall-clock лимит данного этапа/общий бюджет во время этого этапа. Это не утверждение о причине ожидания: возможны SQL, транспорт, нагрузка и т.д.
- SQLSTATE57014 обозначает отмену запроса: неоднозначный случай получает CANCELLED. Только явное первичное сообщение psycopg о statement timeout классифицируется TIMEOUT. Сырые сообщения не выводятся.
- `CONTAINER_START:TIMEOUT` — worker-маркеры не получены: возможны Docker startup/transport, ожидание ввода или старый image. Не считать это CONNECT timeout без маркера.
- Ошибка остановки сообщается отдельно; не утверждается, что контейнер остановлен, если inspect это не подтвердил.

Ограничения OpenSSL/pg_isready из предыдущего отчёта остаются актуальными. Успех check-source не гарантирует полного экспорта, прав на все другие объекты, места под архив и успеха последующего read/hash audit.

## Действия владельца HOSTKEY после ревью PR

Не требуется менять `.env`, API-токены, Compose, DNS или работающие db/app/backup. В согласованном порядке получить одобренный commit в `/opt/grit-wms`, не удаляя локальные изменения. Затем:

`python3 deploy/migrate.py build`

Сборка только migration image, без деплоя и перезапуска WMS. Пересобрать обязательно: старый image не имеет worker watchdog/маркеров.

`python3 deploy/migrate.py check-source`

URL вставить в скрытый вопрос, не в argv/shell. Должны появиться безопасные этапы. При отказе сохранить **только stage/category** и имя созданного диагностического контейнера, если остановка не подтверждена. Не публиковать URL, .env, inspect/config целиком, raw traceback.

Вновь созданный контейнер имеет имя `grit-source-check-<uuid>` и label `grit.migration.preflight=<uuid>`, сохраняется остановленным даже после успеха. Не запускать его повторно как сохранённую конфигурацию миграции: реквизиты туда не записаны. Политика удаления таких контейнеров — отдельное решение владельца; текущий инструмент их не удаляет.

Пока причина рабочего отказа неизвестна, не запускать реальное восстановление/деплой. Существующий нулевой partial и старые bundles оставить на месте. Для последующего финального экспорта действуют отдельные правила основной инструкции: новый путь, остановка writers перед финальной копией и проверки SHA/restore.

## Выполненные проверки

- Полный Python pytest: **354 passed, 16 skipped, 6 существующих warnings**. 16 opt-in PostgreSQL tests этим запуском не выполнялись; не заявляются успешными по skip.
- Новые watchdog tests: **12 passed** (входят в354). Реальные локальные process stalls на CONNECT/SNAPSHOT/READ_ACCESS/NATIVE_CONNECT/SNAPSHOT_IMPORT/CLOSE; общий бюджет не сбрасывается прогрессом; native descendant завершается с группой; нормальный результат; неизвестные payload/исключения не раскрываются; host Docker-CLI stall с безопасным этапом; UUID-label stop/отказ постороннему label; неопределённая остановка; Ctrl+C; SQL startup limits до snapshot, явное close и безопасный worker SIGINT без traceback.
- `deploy/verify_preflight_deadlines.py`: **PASS**, настоящий изолированный PostgreSQL16 TLS → PostgreSQL17. Искусственные Unicode-строки, orders/scans/activities/events и sequence. Проверены полный dump/TOC/SHA/restore, row hashes/schema/constraints, source unchanged, следующий sequence=42.
- В том же Docker-стенде намеренно выполнен блокирующий `pg_sleep` в SNAPSHOT: supervisor вернул `SNAPSHOT:TIMEOUT` за <3s при тестовом лимите0.2s. Проверены все пять обязательных маркеров и отсутствие bundle после check-source.
- Проверен настоящий host reader + scoped Docker stop на том же **вновь созданном** labeled tool container: worker успешно завершился, host подтвердил stopped, container retained. Unit test отдельного зависания Docker CLI использует локальный Python-процесс, не рабочий Docker.
- Migration Docker image build — PASS для основных watchdog/SQL изменений. Последний отдельный обработчик KeyboardInterrupt в entrypoint проверен unit-тестом; пересборка контейнера после его добавления не повторялась из-за заполнения root filesystem. TLS не отключался; synthetic CA только на изолированном стенде. Новых зависимостей нет.
- Node FBS outbox — PASS, replenishment focus — 8 checks PASS. Складские модули и production Compose не менялись.

Все три созданных контейнера, сеть и bind-каталоги синтетических БД/архива сохранены; применялся только stop. Артефакты: `/tmp/grit-deadline-7xl7i2he`, сеть/именной префикс `grit-deadline-abb7072c833d45f1a3d90dd4dd89ec11`, tool `grit-source-check-abb7072c833d45f1a3d90dd4dd89ec11`. Логи: `/tmp/grit-p0-build.log`, `/tmp/grit-p0-integration.log`, `/tmp/grit-p0-pytest.log`.

Ограничение облачной среды: после сохранения контейнеров заполнился root filesystem. Для завершения локальная рабочая копия и venv перенесены в `/tmp` с сохранением прежних путей через symlink. Существующие контейнеры, базы и архивы не удалялись; попытка освобождения только неиспользуемого локального build cache/images их не затрагивала. Итоговый полный прогон выполнен заново из правильного каталога проекта и прошёл. Удалённый GitHub Actions и реальные HOSTKEY/Render не проверялись.
