# GRIT WMS: совместимость PR №92–96 и порядок внедрения

Дата проверки: **6 ноября 2026, Europe/Moscow**. Проверка выполнена в облачной
Linux-среде Codex, не на HOSTKEY. Render, HOSTKEY, DNS и реальные данные не
изменялись. PR в main не объединялись. Все реальные изменения — коммиты в
feature/integration ветках; DB/S3/TLS проверки использовали искусственные стенды.

## 1. Зафиксированные версии и GitHub Actions

База интеграции: main `ee47d14c2f22e5e1d91b3e30448cbf2ce42de438`.
Интеграционная ветка: `integration/hostkey-pr92-96-20261106`.
Исполняемый совместный код после доработки PR95: merge commit `833782d`;
дальнейшие коммиты отчёта не меняют исполняемый код.

| PR | Содержание | Проверенный актуальный head | Удалённый Actions |
|---|---|---|---|
| [92](https://github.com/kuklenkovviktor-lab/moysklad-pallet/pull/92) | PostgreSQL backup/S3/Telegram | `d2ca4c614f40db9ecfa8d2d3a4e0ac596ed89ffd` | [Checks](https://github.com/kuklenkovviktor-lab/moysklad-pallet/pull/92/checks): недоступны из этой среды |
| [93](https://github.com/kuklenkovviktor-lab/moysklad-pallet/pull/93) | FBS/смены/идемпотентность | `25946781b89a7305b7f42e20a50cab90c6fa3b63` | [Checks](https://github.com/kuklenkovviktor-lab/moysklad-pallet/pull/93/checks): недоступны |
| [94](https://github.com/kuklenkovviktor-lab/moysklad-pallet/pull/94) | Блокировка документа приёмки | `ea05ff7d7991f2e1b76f3e3c87afa09e19fac177` | [Checks](https://github.com/kuklenkovviktor-lab/moysklad-pallet/pull/94/checks): недоступны |
| [95](https://github.com/kuklenkovviktor-lab/moysklad-pallet/pull/95) | Миграция + исправленный контроль обновления | `1d694f759eb8b8183595db1ae611cc59b444871e` (runtime fix `26f42c4`, был `815d0f9`) | [Checks](https://github.com/kuklenkovviktor-lab/moysklad-pallet/pull/95/checks): повторно недоступны и для нового head |
| [96](https://github.com/kuklenkovviktor-lab/moysklad-pallet/pull/96) | Необязательный HTTPS proxy | `58aaa8b71640dc49e711befd15116e4cb31e3f83` | [Checks](https://github.com/kuklenkovviktor-lab/moysklad-pallet/pull/96/checks): недоступны |

Каждый head прочитан через refs/pull/N/head. Для каждого SHA выполнен запрос
REST check-runs; для PR92 также GraphQL. Все получили Forbidden. Веб-страница
checks из этой среды вернула 404. Поэтому **ни success, ни failure, ни завершение
удалённого CI не подтверждены**. Ошибка доступа — не ошибка workflow. Анализ
причин удалённых CI failures невозможен без их результатов/логов. Не считать
локальные проверки заменой GitHub Actions; владелец проверяет таблицу перед merge.

## 2. Конфликты и зависимости

Все пять актуальных heads объединены локальными merge в отдельную ветку, затем
добавлен исправленный head PR95. Git-конфликтов нет. Попарные пересечения
изменённых файлов PR также отсутствуют. Это не доказывает семантическую
совместимость, поэтому выполнены проверки ниже.

- PR92 меняет только backup/config/tests/docs; он сохраняет старые архивы,
  проверяет схему до dump и использует отдельный сервис S3.
- PR93 меняет wms.py, FBS pending/skip и импорт PDF. Добавляется
  **fbs_scan_requests**, а старые данные/настройки сотрудников сохраняются.
- PR94 оборачивает API документа приёмки в per-document lock. Он использует
  PostgreSQL того же развёртывания и не конфликтует с per-shift lock PR93.
  Внешние/ручные записи МойСклад или другая PostgreSQL этим lock не защищены.
- PR95 — явные инструменты, не startup migration и не автоматический перенос.
  Полное сравнение до/после restore строгое; maintenance-сравнение допускает
  только точное пустое дополнение PR93. Ops использует ту же внутреннюю сеть
  и существующую grit, без второго PostgreSQL и без адресов Render.
- PR96 — overlay, не активируемый обычным compose.yaml. Он убирает host-порт app,
  публикует только proxy и не открывает PostgreSQL. Его нельзя включать в первый
  application-only этап: это отдельное разрешение DNS/HTTPS/публичного доступа.

## 3. Найденный пробел и исправления PR95

Строгое сравнение схем после старта нового app отклоняло бы штатное добавление
fbs_scan_requests. Просто игнорировать все отличия опасно: так можно скрыть
изменение данных/бэйджей или неожиданную миграцию. Добавлены:

- `compare-live`: все прежние таблицы/схемы/строки/хеши/constraints/indexes должны
  совпасть; единственное новое допустимое состояние — точная ПУСТАЯ таблица PR93.
  Если ledger существовал раньше, его данные тоже сохраняются строго.
- `compose.ops.yaml` / ops_entrypoint.py: только существующая db:5432/grit,
  корректное экранирование пароля libpq без вставки в URI; source Render отвергается.
  Профиль сам ничего не запускает, инструменты вызываются вручную с --no-deps.
- Rehearsal без пересоздания db с сохранением secret files, старого архива,
  custom/disabled сотрудника, активной смены, pending и всех baseline fingerprint.
- 6 unit-проверок и 3 новых PostgreSQL-теста; расширен штатный migration runner.
- Ручной runbook с остановкой только app/backup, emergency dump, полным baseline,
  trial restore, startup/data comparison и новым backup без удаления старых.

Исполняемые исправления опубликованы в ветке PR95 коммитом `26f42c4`;
последний head `1d694f7` дополнительно требует полный immutable SHA в runbook.
Остальные четыре heads в этом цикле не изменялись. Сбой функциональных тестов
не обнаружен; исправлен именно недостающий безопасный контроль rollout.

## 4. Фактические результаты

| Проверка | Результат |
|---|---|
| Полный pytest до дополнительных rollout-проверок | 306 passed, 9 skipped |
| Полный pytest итогового совместного дерева | **312 passed, 12 skipped, 6 warnings** |
| 12 skipped миграционных тестов в отдельной PostgreSQL 17 | **12 passed**; не оставлены непроверенными |
| PostgreSQL: FBS / replay / QR / rollback / конкуренция | 14 passed |
| PostgreSQL: приёмка / два increment / проведение | 4 passed, transport МойСклад искусственный |
| Unit backup/deployment | 29 backup tests + 6 rollout tests прошли в полном suite |
| Node: FBS outbox | PASS: lost ACK после commit, reload/replay, 5xx/409, storage refusal |
| Node: фокус сканера пополнения | 8 checks PASS |
| Docker app / backup / migration / ops | Сборки PASS, TLS verification сохранён |
| S3 HTTPS emulation → HEAD/full GET/SHA-256 → restore | PASS |
| Реальное отключение сети тестового S3 | pending/архив сохранены, health fail, recovery без лишнего dump — PASS |
| Неполная PostgreSQL-схема | dump не создаётся — PASS |
| Restart/recreate app/db/backup | Данные стенда сохранены — PASS |
| Pre-PR93 полный GRIT dump → restore → app → compare-live | ready=true, разрешено только штатное пустое дополнение — PASS |
| Application-only rollout rehearsal | DB container ID прежний, env-файлы byte-identical, старый архив и все baseline записи сохранены — PASS |
| HTTPS fixture с проверяемым test CA | TLS/redirect/headers/camera permission/Secure cookie/служебные deny/XFF — PASS |
| Production Caddyfile | validate без сети — PASS |
| HTTPS compose overlay | app/db не публикуют host ports — PASS |
| Ручной runbook | Bash-синтаксис 8 блоков PASS; на HOSTKEY команды не выполнялись |

Warnings относятся к PyMuPDF/SWIG и helper-функции TSPL, собранной pytest как
тест. Это не падения. Физическая печать/камера/телефон не запускались. Реальные
Yandex, Telegram, ACME и МойСклад не вызывались. S3/TLS работали на отдельных
Docker-сетях с вымышленными ключами и временными сертификатами.
Старая схема моделировалась удалением только своей пустой fixture-таблицы
в новой синтетической БД; runbook не удаляет таблицы или данные пользователя.

Для повторения всего набора добавлен workflow
`.github/workflows/hostkey-integration.yml`. Он выполняет проверенные локально
команды, не имеет deployment credentials и ничего не развёртывает. Удалённый
запуск этого нового workflow ещё не подтверждён.

## 5. Рекомендуемый порядок объединения

**Только после проверки внешнего CI и review. Merge не означает deploy.**

1. PR92 — сохранность/очереди backup без старого pruning.
2. PR95 **последний head 1d694f7** — baseline/trial/maintenance verification до
   изменения работающей схемы. Полный rehearsal требует уже весь набор PR.
3. PR93 — FBS/смены, сохранение сотрудников и добавление request ledger.
4. PR94 — защита количества и проведения документа приёмки.
5. PR96 — последним как необязательная HTTPS-конфигурация, без её активации.

PR93/94 независимы по файлам; относительный порядок технически несущественен,
но единый release проверять целиком. На HOSTKEY не раскатывать промежуточные
main commits после каждого merge. Если используется squash/rebase, итоговый
release SHA изменится — повторить CI/интеграцию на нём перед внедрением.

Перед любым merge проверить Render Auto Deploy. Без контроля этого механизма
merge может непреднамеренно обновить рабочий Render, что выходит за разрешённую
задачу. Мы ничего в Auto Deploy не меняли.

## 6. Ручное обновление HOSTKEY

Точный сценарий: [HOSTKEY_PR92_96_UPDATE.md](HOSTKEY_PR92_96_UPDATE.md).

Краткая последовательность: идентифицировать существующий project/db bind;
защитить env и metadata; остановить только HOSTKEY app/backup; прямой emergency
pg_dump без старого pruning; checkout точного approved SHA; собрать app/backup/ops;
export baseline и trial restore; пересоздать только app; compare-live; новая
копия и новый backup; повторно проверить DB ID и env. PostgreSQL не pull/recreate,
PGDATA не копировать/чистить. HTTPS и Render cutover — отдельные этапы.

Важно: старый app может сбрасывать настройки seed-сотрудников. Поэтому безопасный
откат не равен безусловному запуску старого образа. При расхождении остановить
app/backup, сохранить исходную БД и все архивы; старую версию предварительно
испытать на отдельной копии либо подготовить исправленный rescue release.

## 7. Блокирующие условия

- Из этой среды не подтверждён удалённый CI пяти PR, включая новый PR95 head.
- Реальное содержимое HOSTKEY DB, свободный диск, runtime env/mounts/overlays
  не обследовались. Runbook останавливается при любом несовпадении с исходными
  условиями, дубликатах, orphan-записях или unresolved приёмках.
- Render Auto Deploy требует проверки владельцем до merge; production source
  Render не читался и не переносился.
- Предсказуемые legacy-форматы бейджей остаются риском публичного запуска.
  Требуется решение о compatibility/переиздании, не молчаливое отключение входа.
- Реальные S3/KMS/Telegram, публичный ACME/firewall/DNS и устройства требуют приёмки.
  При раздельных БД или внешних изменениях МойСклад locks не заменяют единственного writer.
- Manifest покрывает public-таблицы/расширения, не произвольные роли/ACL/functions/
  внешние схемы. Если они используются, нужен отдельный compatibility review.

**Вывод:** пять PR совместимы на проверенном локальном стенде; Git-конфликтов и
падений доступных тестов нет. Production-внедрение пока нельзя объявить разрешённым:
сначала закрыть условия выше и согласовать ручной maintenance этап. Все реальные
сервисы и данные сохранены без изменений.
