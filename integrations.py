"""Read-only integration registry for GRIT WMS.

Phase one intentionally does not accept or persist API secrets. Live integrations
remain controlled by their existing environment/configuration and services.
"""
import os
from html import escape

from flask import Blueprint, Response, jsonify, redirect

from wms import manager_access

integrations_bp = Blueprint("integrations", __name__)


def _catalog():
    return [
        {
            "id": "moysklad",
            "name": "МойСклад",
            "purpose": "Каталог, документы, приёмка и складские операции",
            "configured": bool(os.getenv("MOYSKLAD_TOKEN", "").strip()),
            "adapter": "existing",
        },
        {
            "id": "wildberries",
            "name": "Wildberries",
            "purpose": "FBS-заказы, поставки и статусы в рамках прав API",
            "configured": bool(os.getenv("WB_API_TOKEN", "").strip()),
            "adapter": "existing",
        },
        {
            "id": "ozon",
            "name": "Ozon",
            "purpose": "Отправления, товары и операции",
            "configured": False,
            "adapter": "planned",
        },
        {
            "id": "yandex",
            "name": "Яндекс Маркет",
            "purpose": "Заказы, поставки и статусы",
            "configured": False,
            "adapter": "planned",
        },
        {
            "id": "chestny_znak",
            "name": "Честный ЗНАК",
            "purpose": "КИЗ, Data Matrix, статусы маркировки и документы",
            "configured": False,
            "adapter": "planned",
        },
    ]


@integrations_bp.get("/api/manager/integrations")
def integrations_list_api():
    if not manager_access():
        return jsonify(error="Доступ только руководителю"), 403
    return jsonify(integrations=_catalog(), secrets_exposed=False)


@integrations_bp.get("/manager/integrations")
def integrations_page():
    if not manager_access():
        return redirect("/manager-login")
    cards = []
    for item in _catalog():
        if item["adapter"] == "existing":
            status = "Действующие настройки найдены" if item["configured"] else "Ключ не настроен"
            details = "Использует существующую конфигурацию GRIT. Проверка соединения и управление ключами пока не включены."
        else:
            status = "Адаптер запланирован"
            details = "Интеграция ещё не реализована. Подключение и ввод ключей станут доступны после разработки адаптера."
        cards.append(
            '<article class="card"><div class="between"><h2>' + escape(item["name"]) +
            '</h2><span class="status">' + escape(status) + '</span></div><p>' +
            escape(item["purpose"]) + '</p><small>' + escape(details) + '</small></article>'
        )
    html = """<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>GRIT WMS · Интеграции</title>
<style>
*{box-sizing:border-box}body{margin:0;background:#e8edf2;color:#203247;font:15px Arial,sans-serif}
main{max-width:1050px;margin:32px auto;padding:0 18px}
header{display:flex;justify-content:space-between;align-items:center;gap:18px;flex-wrap:wrap}
a{color:#345a73;text-decoration:none;font-weight:700}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(290px,1fr));gap:14px;margin:22px 0}
.card{background:#f8fafc;border:1px solid #d6e0e7;border-radius:12px;padding:20px}
.between{display:flex;align-items:start;justify-content:space-between;gap:8px;flex-wrap:wrap}
h1{margin:8px 0}h2{margin:0;font-size:19px}.status{background:#e7edf2;border-radius:6px;padding:6px;font-size:12px}
p{line-height:1.5}small,.muted{color:#607388;line-height:1.5}.notice{padding:14px;border-left:3px solid #48647a;background:#f8fafc}
</style></head><body><main><header><div><div class="muted">GRIT WMS / Настройки</div>
<h1>Интеграции</h1></div><a href="/manager">← Панель управления</a></header>
<div class="notice">Первый этап: реестр интеграций. Токены не отображаются и не сохраняются на этой странице.
Новые сервисы нельзя подключить до реализации и проверки их адаптеров. Уже работающие соединения не изменены.</div>
<section class="grid">""" + "".join(cards) + """</section></main></body></html>"""
    return Response(html, content_type="text/html; charset=utf-8")
