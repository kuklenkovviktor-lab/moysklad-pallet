"""Attendance journal with two static action QR codes and existing employee badges.

Attendance never starts or closes a production picking shift.
All data changes are designed for isolated synthetic tests first.
"""
import base64
import io
import secrets
import uuid
from datetime import datetime, timezone, timedelta
from html import escape
from urllib.parse import urlparse

from flask import Blueprint, Response, request, session, redirect

from wms import db_connect, get_session_employee, sql

attendance_bp = Blueprint("attendance", __name__)
MSK = timezone(timedelta(hours=3), "MSK")
STYLES = """
:root{color-scheme:dark;font-family:system-ui;background:#09121e;color:#f1f6ff}
*{box-sizing:border-box}body{margin:0;padding:20px;min-height:100vh;background:#09121e}
main{max-width:1100px;margin:auto}.panel{padding:18px;margin:15px 0;border-radius:12px;
background:#142337;border:1px solid #30465c}h1{margin:0 0 10px}
a{color:#8dccff}p,small{color:#9eb5ca}
button,input,select{background:#0d1d2c;color:#f1f6ff;border:1px solid #42607a;
border-radius:9px;padding:10px;min-height:42px}button{background:#176eae;cursor:pointer}
button:focus-visible,a:focus-visible{outline:2px solid #ff9230}
table{border-collapse:collapse;width:100%}td,th{border-bottom:1px solid #30465c;padding:9px;text-align:left}
th{color:#9eb5ca}.scroll{overflow-x:auto}.qr{background:white;padding:12px;border-radius:9px;width:220px}
/* Same GRIT translucent navigation capsule as the desktop shared theme. */
.grit-nav-pill{display:inline-flex;align-items:center;justify-content:center;gap:8px;
min-height:44px;padding:10px 18px;border-radius:9999px;
text-decoration:none;color:#f1f6ff;background:rgba(126,164,199,.12);
border:1px solid rgba(170,204,237,.22);-webkit-backdrop-filter:blur(12px);backdrop-filter:blur(12px);
transition:background-color .18s ease,border-color .18s ease}
.grit-nav-pill:hover{background:rgba(126,164,199,.22);border-color:rgba(170,204,237,.36);color:#fff}
.grit-nav-pill:focus-visible{outline:2px solid #ff9230;outline-offset:2px}
@media(prefers-reduced-motion:reduce){.grit-nav-pill{transition:none}}
@media print{body,.panel{background:#fff;color:#000;border:none}nav,button,.grit-nav-pill{display:none}}
"""


# Attendance QR printing is strictly 58 x 40 mm: two labels, two pages.
QR_PRINT_CSS = """
@page{size:58mm 40mm;margin:0}
@media print{
 html,body,main{width:58mm!important;min-width:58mm!important;max-width:58mm!important;
  margin:0!important;padding:0!important;background:#fff!important;color:#000!important}
 body{overflow:visible!important}
 main{display:block!important}
 nav{display:none!important}
 body section.attendance-label{display:flex!important;flex-direction:column!important;
  align-items:center!important;justify-content:center!important;gap:0.7mm!important;
  box-sizing:border-box!important;width:58mm!important;height:40mm!important;
  min-height:40mm!important;max-height:40mm!important;margin:0!important;
  padding:1.5mm 2mm!important;border:0!important;border-radius:0!important;
  background:#fff!important;color:#000!important;box-shadow:none!important;
  break-inside:avoid!important;page-break-inside:avoid!important;
  break-after:page;page-break-after:always;overflow:hidden!important}
 body section.attendance-label:last-child{break-after:auto;page-break-after:auto}
 body section.attendance-label h1{font:700 11pt/1.1 Arial,sans-serif!important;
  color:#000!important;margin:0!important;padding:0!important}
 body section.attendance-label img.qr{display:block!important;width:25mm!important;
  height:25mm!important;max-width:25mm!important;max-height:25mm!important;
  padding:0!important;margin:0!important;border:0!important;border-radius:0!important;
  object-fit:contain!important;background:#fff!important}
 body section.attendance-label p{font:6pt/1.1 Arial,sans-serif!important;
  color:#000!important;margin:0!important;padding:0!important}
}
"""

def page(title, body, status=200, extra_css=""):
    doc = ('<!doctype html><html lang="ru"><head><meta charset="utf-8">'
           '<meta name="viewport" content="width=device-width,initial-scale=1">'
           '<meta name="theme-color" content="#09121e"><title>GRIT · '
           + escape(title) + '</title><style>' + STYLES + extra_css + '</style></head>'
           '<body><main>' + body + '</main></body></html>')
    return Response(doc, status=status, mimetype="text/html",
                    headers={"Cache-Control": "no-store"})

def init_attendance_db():
    # Adds independent tables; does not modify employees, badges or shifts.
    with db_connect() as conn:
        c = conn.cursor()
        c.execute("""CREATE TABLE IF NOT EXISTS attendance_profiles (
            employee_id TEXT PRIMARY KEY REFERENCES employees(id),
            employment_status TEXT NOT NULL DEFAULT 'staff',
            enabled INTEGER NOT NULL DEFAULT 1
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS attendance_visits (
            id TEXT PRIMARY KEY,
            employee_id TEXT NOT NULL REFERENCES employees(id),
            arrived_at TEXT NOT NULL,
            left_at TEXT,
            source TEXT NOT NULL DEFAULT 'qr'
        )""")
        c.execute("""CREATE UNIQUE INDEX IF NOT EXISTS attendance_one_open
                     ON attendance_visits(employee_id) WHERE left_at IS NULL""")
        c.execute("""CREATE TABLE IF NOT EXISTS attendance_audit (
            id TEXT PRIMARY KEY, employee_id TEXT NOT NULL,
            action TEXT NOT NULL, detail TEXT NOT NULL,
            reason TEXT NOT NULL, changed_at TEXT NOT NULL
        )""")
        conn.commit()
        c.close()

def _token():
    if "attendance_csrf" not in session:
        session["attendance_csrf"] = secrets.token_urlsafe(32)
    return session["attendance_csrf"]

def _csrf_ok(token):
    if not secrets.compare_digest(str(token or ""), str(session.get("attendance_csrf") or "invalid")):
        return False
    origin = request.headers.get("Origin")
    if origin:
        p = urlparse(origin)
        return p.scheme == "https" and p.netloc == request.host
    return True

def _manager():
    # Do not allow a manager's terminal badge to access the desktop register.
    return session.get("manager_web") is True

def _staff(employee_id):
    with db_connect() as conn:
        c=conn.cursor()
        c.execute(sql("""SELECT e.id,e.name,e.role,e.active,
            COALESCE(p.employment_status,'staff'),COALESCE(p.enabled,1)
            FROM employees e LEFT JOIN attendance_profiles p ON p.employee_id=e.id
            WHERE e.id=?"""), (employee_id,))
        row=c.fetchone()
        c.close()
    return row if row and row[3] == 1 and row[2] != "manager" else None

def _stamp():
    return datetime.now(timezone.utc).isoformat()

def _format_time(raw):
    if not raw:
        return "—"
    return datetime.fromisoformat(raw.replace("Z","+00:00")).astimezone(MSK).strftime("%d.%m %H:%M")

@attendance_bp.get("/attendance/<action>")
def worker_page(action):
    if action not in ("in", "out"):
        return Response("Not found", 404)
    emp=get_session_employee()
    if not emp:
        return redirect("/login?next=/attendance/"+action)
    init_attendance_db()
    person=_staff(emp["id"])
    if not person or not person[5]:
        return page("Отметка недоступна", '<section class="panel"><h1>Учёт посещаемости отключён</h1></section>',403)
    label = "ВХОД" if action=="in" else "ВЫХОД"
    # Visiting the QR page never writes by GET. One same-origin CSRF POST follows
    # automatically; a manual fallback remains if JavaScript is disabled.
    # One invisible same-origin CSRF form submission; only failures display
    # a message. Successful POST responses redirect to the correct terminal.
    body=('<form id="attendance-action" method="post" action="/attendance/'+action+'">'
          '<input type="hidden" name="csrf" value="'+escape(_token())+'">'
          '<noscript><button>Зафиксировать '+label.lower()+'</button></noscript>'
          '</form>'
          '<script>document.getElementById("attendance-action").requestSubmit();</script>')
    return page(label,body,extra_css='main{visibility:hidden}')

@attendance_bp.post("/attendance/<action>")
def worker_mark(action):
    if action not in ("in","out"):
        return Response("Not found",404)
    emp=get_session_employee()
    if not emp:
        return redirect("/login?next=/attendance/"+action)
    if not _csrf_ok(request.form.get("csrf")):
        return Response("Недействительная форма",403)
    init_attendance_db()
    person=_staff(emp["id"])
    if not person or not person[5]:
        return Response("Учёт отключён",403)
    from wms import DATABASE_URL
    from fbs_transactions import event
    with db_connect() as conn:
        cur=conn.cursor()
        try:
            if not DATABASE_URL:
                cur.execute("BEGIN IMMEDIATE")
            # Serialize attendance and shift transitions for this employee.
            cur.execute(sql("SELECT id FROM employees WHERE id=?" +
                            (" FOR NO KEY UPDATE" if DATABASE_URL else "")), (emp["id"],))
            cur.fetchone()
            cur.execute(sql("""SELECT id FROM attendance_visits WHERE employee_id=?
                AND left_at IS NULL LIMIT 1"""), (emp["id"],))
            open_visit=cur.fetchone()
            if action=="in" and open_visit:
                # Idempotent: the attendance visit already exists; no duplicate insert.
                message,status="Приход уже отмечен",200
            elif action=="out" and not open_visit:
                message,status="Нет открытого прихода, обратитесь к руководителю",409
            elif action=="in":
                cur.execute(sql("""INSERT INTO attendance_visits
                    (id,employee_id,arrived_at,source) VALUES(?,?,?,'qr')"""),
                    (str(uuid.uuid4()),emp["id"],_stamp()))
                message,status="Приход отмечен",200
            else:
                stamp=_stamp()
                cur.execute(sql("UPDATE attendance_visits SET left_at=? WHERE id=? AND left_at IS NULL"),
                            (stamp,open_visit[0]))
                cur.execute(sql("""SELECT id FROM shifts WHERE employee_id=?
                    AND ended_at IS NULL ORDER BY started_at DESC LIMIT 1""" +
                    (" FOR UPDATE" if DATABASE_URL else "")), (emp["id"],))
                shift=cur.fetchone()
                if shift:
                    cur.execute(sql("UPDATE activities SET ended_at=? WHERE shift_id=? AND ended_at IS NULL"),
                                (stamp,shift[0]))
                    cur.execute(sql("UPDATE shifts SET ended_at=? WHERE id=? AND ended_at IS NULL"),
                                (stamp,shift[0]))
                    event(cur,emp["id"],shift[0],"shift","end",result="attendance-out")
                message,status="Уход отмечен. Смена завершена.",200
            if status==200:
                conn.commit()
            else:
                conn.rollback()
        except Exception:
            conn.rollback()
            message,status="Не удалось завершить отметку. Повтори сканирование.",409
        finally:
            cur.close()
    if status==200:
        if action=="out":
            session.clear()
            # The login page clears worker-specific localStorage on terminal.
            return redirect("/login?attendance_exit=1", code=303)
        return redirect("/fbs", code=303)
    return page("Посещаемость",'<section class="panel"><h1>'+escape(message)
                +'</h1><p>'+escape(person[1])+'</p></section>',status)

@attendance_bp.get("/manager/attendance")
def journal():
    if not _manager():
        return redirect("/manager-login")
    init_attendance_db()
    day=request.args.get("day") or datetime.now(MSK).strftime("%Y-%m-%d")
    try:
        selected=datetime.strptime(day,"%Y-%m-%d").replace(tzinfo=MSK)
    except ValueError:
        return Response("Неверная дата",400)
    start=selected.astimezone(timezone.utc).isoformat()
    end=(selected+timedelta(days=1)).astimezone(timezone.utc).isoformat()
    with db_connect() as conn:
        cur=conn.cursor()
        cur.execute(sql("""SELECT e.id,e.name,COALESCE(p.employment_status,'staff'),
            COALESCE(p.enabled,1),v.arrived_at,v.left_at
            FROM employees e
            LEFT JOIN attendance_profiles p ON p.employee_id=e.id
            LEFT JOIN attendance_visits v ON v.employee_id=e.id
                AND v.arrived_at>=? AND v.arrived_at<?
            WHERE e.role!='manager' AND e.active=1 ORDER BY e.name,v.arrived_at"""),
            (start,end))
        rows=cur.fetchall();cur.close()
    formtoken=escape(_token())
    parts=['<nav><a class="grit-nav-pill" href="/manager">← Панель руководителя</a></nav>',
           '<section class="panel"><h1>Журнал посещаемости</h1>',
           '<p>Вход/выход не зависят от производственной смены. Время: Москва.</p>',
           '<form method="get"><input type="date" name="day" value="'+escape(day)+'">'
           '<button>Показать</button> <a href="/manager/attendance/qr">Печать двух QR</a></form>',
           '<div class="scroll"><table><tr><th>Сотрудник</th><th>Приход</th>'
           '<th>Уход</th><th>Категория и учёт</th></tr>']
    for eid,name,st,enabled,arrived,left in rows:
        parts.append('<tr><td>'+escape(name)+'</td>'
                     '<td>'+escape(_format_time(arrived))+'</td>'
                     '<td>'+escape(_format_time(left))+'</td><td>'
                     '<form method="post" action="/manager/attendance/profile">'
                     '<input type="hidden" name="csrf" value="'+formtoken+'">'
                     '<input type="hidden" name="employee_id" value="'+escape(eid)+'">'
                     '<input type="hidden" name="day" value="'+escape(day)+'">'
                     '<select name="status"><option value="staff"'+(' selected' if st=="staff" else '')+'>Штатный</option>'
                     '<option value="trainee"'+(' selected' if st=="trainee" else '')+'>Стажёр</option></select>'
                     '<label><input type="checkbox" name="enabled" value="1"'
                     +(' checked' if enabled else '')+'> Учитывать</label> '
                     '<button>Сохранить</button></form></td></tr>')
    parts.append('</table></div></section>')
    return page("Журнал",''.join(parts))

@attendance_bp.post("/manager/attendance/profile")
def change_profile():
    if not _manager():
        return Response("Доступ запрещён",403)
    if not _csrf_ok(request.form.get("csrf")):
        return Response("Недействительная форма",403)
    init_attendance_db()
    eid=request.form.get("employee_id","")
    status=request.form.get("status","")
    enabled=request.form.get("enabled")=="1"
    if status not in ("staff","trainee") or not _staff(eid):
        return Response("Неверные данные",400)
    with db_connect() as conn:
        cur=conn.cursor()
        cur.execute(sql("""INSERT INTO attendance_profiles(employee_id,employment_status,enabled)
            VALUES(?,?,?) ON CONFLICT(employee_id) DO UPDATE SET
            employment_status=excluded.employment_status,enabled=excluded.enabled"""),
            (eid,status,int(enabled)))
        cur.execute(sql("""INSERT INTO attendance_audit
            (id,employee_id,action,detail,reason,changed_at)
            VALUES(?,?,?,?,?,?)"""),
            (str(uuid.uuid4()),eid,"profile",status+":"+str(enabled),"manager change",_stamp()))
        conn.commit();cur.close()
    day=request.form.get("day","")
    return redirect("/manager/attendance?day="+escape(day) if len(day)==10 else "/manager/attendance",303)

@attendance_bp.get("/manager/attendance/qr")
def print_qr():
    if not _manager():
        return redirect("/manager-login")
    import qrcode
    sections=[]
    # The action URL is constant; existing badge session identifies the person.
    for action,label in (("in","ВХОД"),("out","ВЫХОД")):
        qr=qrcode.make("https://gritwms.space/attendance/"+action)
        output=io.BytesIO();qr.save(output,format="PNG")
        image="data:image/png;base64,"+base64.b64encode(output.getvalue()).decode("ascii")
        sections.append('<section class="panel attendance-label" style="display:inline-flex;flex-direction:column;align-items:center;justify-content:center;text-align:center;margin:12px;padding:12px">'
                        '<h1>'+label+'</h1><img class="qr" src="'+image+'" alt="'+label+'">'
                        '<p>GRIT WMS · '+label+'</p></section>')
    return page("QR посещаемости",'<nav><button onclick="window.print()">Печать QR</button> '
                '<a class="grit-nav-pill" href="/manager/attendance">← Назад</a></nav>'+''.join(sections), extra_css=QR_PRINT_CSS)
