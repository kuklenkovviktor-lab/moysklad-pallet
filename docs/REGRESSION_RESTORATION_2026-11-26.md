# Восстановление регрессионных тестов — 26.11.2026

База: `main` **3935c9ef1753f1ea599f2ed2626e0e68bfa20699** (PR #190).
Ветка: `test/restore-attendance-regressions`. Источник задачи — аудит PR #195.
Изменены только тесты, тестовая инфраструктура и этот отчёт. Изменения #191/#194/#195 не включены.

## Результаты

| Код приложения | Тесты | PASS | FAIL | ERROR | SKIP |
|---|---|---:|---:|---:|---:|
| Исходная main, отдельный git archive | Исходный набор аудита | 56 | 20 | 0 | 0 |
| Исходная main, отдельный git archive | Исправленные тесты + 6 новых проверок доступа | 82 | 0 | 0 | 0 |
| Текущая ветка | Тот же исправленный набор | 82 | 0 | 0 | 0 |
| Исходная main | Дополнительно: test_wb_fbs_dashboard_queue.py | 8 | 0 | 7 | 0 |
| Текущая ветка | Тот же дополнительный WB-набор | 8 | 0 | 7 | 0 |

Семь дополнительных ERROR воспроизведены на main: `PermissionError: [WinError 32]` при удалении `queue.sqlite` в `tearDown`. Они не относятся к исходным 20 падениям и не исправлены в этом PR. В тесте используются `with sqlite3.connect(...)` без явного `close`: такой контекст завершает транзакцию, но не закрывает соединение. Имена: `test_assembly_admission_day_not_created_day`, `test_current_confirm_backlog_and_separate_delivery`, `test_current_confirm_ignores_creation_day_and_tracks_unadmitted`, `test_empty_queue_is_real_zero`, `test_failure_is_not_displayed_as_zero`, `test_invalid_admission_day_rejected`, `test_manager_required`. Это отдельная задача переносимости тестовых фикстур; ошибки не подавлялись.

## Каждое из 20 исходных падений

В строках 4–19 файл `tests/test_receiving_confirmation.py`, класс `ReceivingConfirmationTests`; в именах опущен общий префикс `test_`. Все исходные проверки результатов операций сохранены.

| № | Тест | Причина на main | Исправление и проверяемый результат |
|---:|---|---|---|
| 1 | test_grit_dashboard_theme.py::test_dashboard_keeps_live_data_bindings_and_existing_controls | Удалённые planInput/savePlan/openExitQr и daily-plan | Текущие WB-поля, picking-queue и ссылка /manager/attendance/qr; PASS |
| 2 | test_grit_dashboard_polish.py::test_existing_warehouse_operations_remain_connected | Те же старые контролы, refreshExitQr | Действующий loadWbAssemblyPlan, счётчики и QR-ссылка; остальные связи сохранены; PASS |
| 3 | MobileNavigationTests.test_replenishment_requires_badge_and_loads_with_employee | Cookie employee_id без посещения → 302 вместо 200 | Настоящий QR + бейдж → 200; анонимный запрос по-прежнему 302 /login; PASS |
| 4 | deleted_receipt_cannot_be_completed | Attendance 401 до обработчика | QR + бейдж → 409 deleted; complete не вызывается; PASS |
| 5 | deleted_receipt_rejects_pallet_without_replaying | Attendance 401 | QR + бейдж → deleted/rejected, без повторной записи; PASS |
| 6 | deleted_receipt_status_blocks_reopening | Attendance 401 | QR + бейдж → 409 deleted; PASS |
| 7 | draft_receipt_remains_accessible | Attendance 401 | QR + бейдж → доступный черновик, 200; PASS |
| 8 | moysklad_412_trash_is_not_recorded_as_uncertain | Attendance 401 | Синтетический HTTP 412 → 409 deleted, операция rejected; PASS |
| 9 | next_physical_pallet_same_barcode_new_uuid | Attendance 401 | Два разных UUID вызывают две разрешённые записи; PASS |
| 10 | osz_nonfinite_quantity_rejected | Attendance 401 вместо 400 | Вход не отменяет проверку недопустимого количества; PASS |
| 11 | osz_same_sscc_double_scan_never_adds_twice | Attendance 401 | Первый приём 200, повтор 409, ровно одна запись; PASS |
| 12 | osz_timeout_blocks_same_sscc_and_receipt_completion | Attendance 401 | Тайм-аут 503/uncertain, повтор и завершение документа 409; PASS |
| 13 | osz_two_devices_compete_for_one_sscc | Первый клиент не доходил до записи; timeout Event | Оба клиента вошли штатно; реальная конкуренция SQLite, один 200, другой 409, одна запись; PASS |
| 14 | partner_pallet_confirm_once_even_with_new_uuid | Attendance 401 | Подтверждение палеты не повторяет запись с новым UUID; PASS |
| 15 | partner_pallet_preview_and_quantity_guard | Attendance 401 | Предпросмотр и проверка количества достигают обработчика; PASS |
| 16 | scan_requires_operation_id | Attendance 401 вместо 400 | После входа отсутствие UUID по-прежнему отклоняется; PASS |
| 17 | single_confirm_then_retry_is_idempotent | Attendance 401 | Два 200, второй replayed; один lookup/write/audit, статус done; PASS |
| 18 | timeout_retry_does_not_write_again_and_can_be_reconciled | Attendance 401 | 503 → 409, блок завершения, ручная сверка → reconciled; PASS |
| 19 | uuid_cannot_be_reused_for_another_receipt_or_quantity | Attendance 401 | После входа защита UUID и параметров сохраняется; PASS |
| 20 | MobileReceivingResumeTests.test_confirmed_only_scoped_to_employee_and_document | Attendance 401 | Настоящий вход; только 2 подтверждения/156 единиц своего сотрудника и документа, без чужих/pending; PASS |

Таким образом, 17 сценариев блокировались middleware до бизнес-обработчиков, один PWA-сценарий — штатным перенаправлением, два проверяли устаревший HTML-контракт.

## Фикстуры и границы безопасности

`tests/attendance_test_support.py` создаёт отдельную временную SQLite на каждый сценарий и синтетического сотрудника **staff**, не manager. Выполняются реальные POST `/api/attendance/entry-intent` с QR своего host и `/api/auth/login` с бейджем. Проверяются открытое посещение и привязка сессии к смене из ответа login. `attendance_verified`, сотрудник и смена в положительных сценариях вручную не устанавливаются; middleware и функции авторизации не подменяются.

Отключён только фоновый инициализатор: схема создаётся синхронно в тестовой БД. Внешние складские адаптеры подменены существующими mocks, а соединения сокетов запрещены. Проверки mock.call_count и состояний receiving_operations доказывают выполнение обработчиков после middleware; реальный МойСклад не вызывался.

Добавлены 6 полных app-тестов: анонимный скан; бейдж без QR/посещения; старый cookie без посещения (401 API и 302 страницы); чужой бейдж на занятом терминале; запрет staff на изменение/печать менеджерских бейджей; запрет чтения/сверки чужой операции. Проверяется отсутствие вызова складской записи при отказе. Сохранены все 22 существующих attendance-теста и 4 проверки manager badge routes.

## Подтверждение dashboard-контракта и отдельный дефект

Контракт подтверждён исходниками main/PR #190: `renderProgress` использует `remaining_orders`, `assembled_assembly_orders`, `assembly_orders`; `loadWbAssemblyPlan` получает `/api/manager/wb-fbs/picking-queue`; новый QR доступен по `/manager/attendance/qr`. Обновлены только устаревшие утверждения тестов, а не HTML/JS. Дополнительный WB-набор проверяет этот API, но его семь Windows cleanup-ошибок учтены выше.

**Отдельный функциональный дефект, не исправленный этим PR:** в конце STATS_PAGE (`wms.py`, исходная строка 2467) остался вызов `setTimeout(openExitQr,250)` при `sessionStorage.openExitQr === '1'`, хотя функция удалена. RECEIVING_MONITOR_PAGE всё ещё устанавливает этот флаг и ведёт на `/manager#qr`. Выполнение исходной строки в Node VM с синтетическим sessionStorage воспроизвело `ReferenceError: openExitQr is not defined`. Это не браузерный end-to-end тест. Новая прямая QR-ссылка существует, но старый путь из монитора приёмки требует отдельного исправления. Утверждение об отсутствии любых ошибок dashboard здесь не делается; дефект не замаскирован ожидаемым успешным результатом.

Воспроизведение дефекта без сервера и данных:

```javascript
const fs = require('fs'), vm = require('vm');
const page = fs.readFileSync('wms.py', 'utf8').split('STATS_PAGE = """')[1].split('"""')[0];
const line = page.split('\n').find(x => x.startsWith("if(sessionStorage.getItem('openExitQr')"));
vm.runInNewContext(line, {
  sessionStorage: {getItem: () => '1', removeItem: () => {}}, setTimeout: () => {}
}); // ReferenceError: openExitQr is not defined
```

## Воспроизведение набора

Из корня репозитория, Python с установленными зависимостями проекта (зависимости не менялись):

```text
python tests/run_attendance_regressions.py --compare-base 3935c9ef1753f1ea599f2ed2626e0e68bfa20699
```

Runner перечисляет весь набор явно, выполняет каждый модуль в отдельном процессе, включая unittest-классы и обычные функции test_*. Для baseline используется временный git archive; затем туда накладываются **только тестовые файлы**, не код приложения. Синтетические env и запрет сети устанавливаются до импорта приложения. Возвращается ненулевой код при FAIL/ERROR/SKIP восстановленного набора; исходные ожидаемые 20 FAIL выводятся отдельно. Для обычного запуска убрать `--compare-base`. `PYTHONPATH` поддерживается для изолированной папки зависимостей.

Проверки синтаксиса изменённых Python-файлов, импортов (в составе запуска) и `git diff --check` выполнены. Production-файлы совпадают с базой.

Это **не полная проверка системы**: PostgreSQL, браузер/PWA на устройстве, камера/HID, печать и реальные интеграции не тестировались. Merge/deploy, GitHub Actions, production-БД и реальные складские операции не выполнялись. Откат при необходимости — revert этого тестового коммита, без изменения данных или конфигурации.
