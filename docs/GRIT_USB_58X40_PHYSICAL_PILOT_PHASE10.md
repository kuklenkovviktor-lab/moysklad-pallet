# GRIT WMS: один физический USB-тест Xprinter XP-460B, этикетка 58x40 мм (phase 10)

Дата: 26.11.2026. Issue #192. Базовый код: draft PR #206.

## Статус и цель

В истории ONIX уже была тестовая отправка диагностических этикеток через Windows RAW/TSPL на второй XP-460B. Но это НЕ доказывает физическую печать через новую очередь GRIT QR -> PostgreSQL -> виртуальный агент. Данный этап проверяет ТОЛЬКО тракт локальный Windows -> выделенный USB XP460B -> одна синтетическая бумажная этикетка 58x40 мм.

Испытание проводить исключительно на отдельном РАЗРЕШЁННОМ тестовом ПК и выделенном тестовом принтере. При отсутствии разрешения на конкретное оборудование остановиться после безаппаратных проверок. Не использовать заказы/принтеры работодателя без разрешения.

## Безопасный контракт

- Единственный standalone файл: services/printing/grit_usb_print_pilot.py. Он не регистрируется в production app.py/wms.py и не подключается к облачному GRIT.
- Никакой сети, WB, PDF-стикеров, production DB, реальных заказов, фонового polling или автопечати.
- Строгое имя Windows-очереди ONIX-TEST-XP460B, не являющейся принтером по умолчанию. Реальный локальный порт должен быть USB001, USB002 и т.п. WSD/TCP/IP/COM запрещены.
- Фиксированная синтетическая TSPL-этикетка: SIZE 58 mm,40 mm; GAP 2 mm,0 mm; GRIT WMS / SYNTHETIC / NOT WB / USB TEST 1/1; QR: GRIT-USB-TEST-ONLY. Это НЕ действительный QR принтера GRIT и НЕ стикер WB.
- Dry-run через --preview, --list, --check, --status. Только явные --printer ONIX-TEST-XP460B, --print-one и --confirm PRINT-ONE-GRIT-58X40 отправляют один пакет.
- До вызова USB создаётся одноразовый локальный журнал. Любой повтор отвергается даже после ошибки Windows или аварии процесса.
- Журнал: LOCALAPPDATA\GRIT-WMS\USB-Pilot\grit_xprinter_58x40_one_attempt.json. Статусы CLAIMED/UNKNOWN/SUBMITTED_TO_WINDOWS. НЕ удалять журнал для обхода блокировки.
- SUBMITTED_TO_WINDOWS означает только передачу всех байтов Windows spooler, НЕ физический отпечаток, НЕ PRINTED.

## A. Безаппаратные тесты на HOSTKEY

    DIR=$(mktemp -d /tmp/grit-usb-pr207-XXXXXX)
    git clone -q --depth 1 --branch feature/pr192-grit-usb-physical-pilot https://github.com/kuklenkovviktor-lab/moysklad-pallet.git "$DIR"
    chmod 755 "$DIR"
    bash "$DIR/deploy/verify_grit_usb_pilot_offline.sh" "$DIR"

Ожидается 16 tests / OK / GRIT USB 58X40 FAKE SPOOLER / NO HARDWARE PASS.

## B. Windows на разрешённом выделенном тестовом компьютере: только проверка

Убедиться, что отдельный тестовый XP-460B заправлен рулоном 58x40, а рабочая FBS-очередь не переименовывалась. Настоящую физическую принадлежность проверять по USB-подключению: название очереди само по себе ещё ничего не доказывает.

В новом PowerShell (НЕ SSH на HOSTKEY):

    $DIR = Join-Path $env:TEMP "grit-usb-pilot-207"
    git clone -q --depth 1 --branch feature/pr192-grit-usb-physical-pilot https://github.com/kuklenkovviktor-lab/moysklad-pallet.git $DIR
    py --version
    py -c "import win32print; print('PYWIN32 READY')"

Если pywin32 отсутствует, на разрешённом тестовом ПК установить его командой: py -m pip install pywin32. Других зависимостей не требуется.

Команды без физической печати:

    py "$DIR\services\printing\grit_usb_print_pilot.py" --preview
    py "$DIR\services\printing\grit_usb_print_pilot.py" --list
    py "$DIR\services\printing\grit_usb_print_pilot.py" --check --printer ONIX-TEST-XP460B
    py "$DIR\services\printing\grit_usb_print_pilot.py" --status

Ожидаемый CHECK: USB CHECK PASS: ONIX-TEST-XP460B port: USB00x. Любая ошибка, неизвестный порт, отсутствие тестового принтера или сомнения в том, какой USB-кабель к чему ведёт, означают STOP.

## C. Одна физическая этикетка только после явной проверки тестового устройства

    py "$DIR\services\printing\grit_usb_print_pilot.py" --printer ONIX-TEST-XP460B --print-one --confirm PRINT-ONE-GRIT-58X40

Программа возвращает SUBMITTED_TO_WINDOWS, но это лишь результат отправки в Windows. Факт физической печати проверяет оператор:

1. Физически ОДНА этикетка 58x40, не обрезана и без перекоса;
2. Видны GRIT WMS, SYNTHETIC, NOT WB, USB TEST 1/1;
3. QR сканером возвращает ровно GRIT-USB-TEST-ONLY;
4. Ни одно рабочее задание FBS не напечаталось, другие устройства не активировались;
5. В Windows spooler нет проблем, результат фиксируется для протокола.

Если не печатает, выводит несколько листов, зависает или результат неопределён: НЕ ПОВТОРЯТЬ и НЕ УДАЛЯТЬ журнал. Сверить очередь Windows и реальное состояние устройства. Новая попытка требует отдельно расследованного и разрешённого испытания с новым ID, а не обхода файла.

## D. Следующий этап, НЕ входящий в этот PR

После подтверждения одной бумажной синтетической этикетки нужно отдельно построить доверенный серверный claim/lease для одного тестового принтера с аутентификацией Windows-агента, привязкой к устройству, проверкой синтетического стикера, запретом дублей и статусами SUBMITTED (спулер), UNKNOWN (неясно), PRINTED (достоверное подтверждение). Никакого автоматического retry после UNKNOWN. Не переиспользовать старый облачный ONIX-токен без пересмотра архитектуры.

Этот PR не подключает USB к демо на HOSTKEY, не импортируется в production и не меняет основную GRIT. Только code review, dry-run, проверка эмулятора и отдельно разрешённая ручная физическая печать.
