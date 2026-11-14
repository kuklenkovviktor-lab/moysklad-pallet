"""Presentation-only views for the ONIX restricted partner portal."""
from datetime import datetime, timedelta, timezone
from html import escape
from urllib.parse import urlencode

CSS = """
:root{--bg:#edf2f6;--paper:#fff;--ink:#1b2d3c;--muted:#718294;--navy:#446278;--line:#e1e8ee;--soft:#f5f8fa;--blue:#e9f3fb}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.48 system-ui,-apple-system,"Segoe UI",sans-serif}
a{color:#315f82;text-decoration:none}a:hover{text-decoration:underline}
.shell{max-width:1200px;padding:22px 24px 58px;margin:0 auto}
.topbar{display:flex;align-items:center;justify-content:space-between;background:#fff;border:1px solid var(--line);border-radius:17px;box-shadow:0 4px 18px #2037480b;padding:15px 20px;gap:16px}
.brand{display:flex;align-items:center;gap:12px}.logo{width:43px;height:43px;display:grid;place-items:center;border-radius:12px;background:#46667e;color:#fff;font-size:13px;font-weight:900;letter-spacing:-.5px}
.brand b{font-size:20px;letter-spacing:.1px}.brand small{display:block;color:var(--muted);font-size:12px}
.account{display:flex;gap:10px;align-items:center;flex-wrap:wrap;justify-content:flex-end}
.partner-pill{border:1px solid #dce6ee;border-radius:9px;padding:8px 13px;color:#365267;background:#f5f8fa;font-weight:650}
h1{font-size:26px;line-height:1.25;margin:23px 0 4px;letter-spacing:-.5px}h2{font-size:18px;line-height:1.3;margin:0 0 7px}
h3{font-size:15px;margin:0 0 14px}
.subtext,.muted{color:var(--muted)}.subtext{margin:0 0 20px}
section,.panel{background:var(--paper);border:1px solid var(--line);border-radius:16px;box-shadow:0 4px 20px #26384a08;padding:22px;margin:18px 0}
.panel p{margin:6px 0}.dashboard-title{margin:27px 0 20px}.dashboard-title h1{margin:0 0 5px}
.kpis{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px}
.kpi{background:#fff;border:1px solid var(--line);border-radius:14px;padding:15px 17px;min-width:0}
.kpi .number{font-size:27px;line-height:1.3;font-weight:800;margin-top:5px;font-variant-numeric:tabular-nums}
.kpi .kpi-label{font-size:12px;color:var(--muted);font-weight:620}
.kpi .number.blue{color:#486e92}.kpi .number.green{color:#248467}.kpi .number.amber{color:#ad6d29}
.forms-grid{display:grid;grid-template-columns:1fr 1fr;gap:15px;align-items:stretch;margin-top:16px}
.forms-grid .panel{margin:0;display:flex;flex-direction:column}
.panel-top{display:flex;align-items:center;gap:10px;margin-bottom:17px}.panel-icon{width:38px;height:38px;background:#edf3f7;color:#42647d;border-radius:10px;display:grid;place-items:center;font-size:19px}
label{display:block;font-size:12px;color:#4c6071;font-weight:700;margin:0 0 6px}
input,select,button{font:inherit}
input:not([type=hidden]),select{width:100%;border:1px solid #d0dae3;border-radius:9px;background:#fff;padding:11px 12px;color:var(--ink);min-height:44px;outline:none}
input:focus,select:focus{border-color:#597e9c;box-shadow:0 0 0 3px #537c9e1b}
input[type=file]{font-size:13px;padding:9px;background:#f8fafb;min-width:0}
.field{margin-bottom:13px;min-width:0}.field-note{font-size:12px;color:var(--muted);margin-top:7px;min-height:18px}
.fields-inline{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.btn{display:inline-flex;align-items:center;justify-content:center;gap:7px;border:1px solid transparent;border-radius:9px;padding:11px 15px;min-height:42px;cursor:pointer;font-weight:700;text-decoration:none;white-space:nowrap}
.btn:hover{text-decoration:none;filter:brightness(.96)}
.btn-primary{background:#48687f;color:#fff}.btn-primary:hover{color:#fff}
.btn-secondary{background:#f4f7f9;color:#3d5b70;border-color:#dce5eb}
.btn-ghost{background:#fff;color:#385d77;border-color:#dbe4eb}
.btn:disabled,button:disabled{opacity:.6;cursor:wait}
.form-foot{margin-top:auto;padding-top:10px}.form-foot .btn{width:100%}
.dropzone{border:1.5px dashed #b6c9d8;border-radius:12px;background:#f7fafc;padding:21px 14px;text-align:center;margin:6px 0 14px}
.dropzone .symbol{font-size:30px;line-height:1.3}.dropzone .filename{font-size:12px;color:#657b8e;margin-top:8px;overflow-wrap:anywhere}
.dropzone input{width:100%;margin-top:12px}
.notice{padding:12px 14px;border-radius:9px;margin:14px 0;background:#eaf4ef;color:#246c53;border:1px solid #cfebdc}
.notice.error{color:#a64d4d;background:#fff1f1;border-color:#f4cdcd}
.toolbar{display:flex;align-items:flex-end;gap:10px;margin:20px 0 15px;flex-wrap:wrap}
.toolbar .field{flex:1;margin:0;min-width:185px}
.toolbar .status-filter{flex:0 1 210px}
.table-wrap{overflow-x:auto}
table{width:100%;border-collapse:collapse}
th{text-align:left;color:#788899;font-size:12px;font-weight:750;letter-spacing:.15px;padding:12px 10px;border-bottom:1px solid var(--line)}
td{padding:13px 10px;border-bottom:1px solid #eaf0f4;vertical-align:middle;font-size:13px}
tbody tr:nth-child(odd){background:#fafcfd}tbody tr:hover{background:#edf5fa}
.article{font-weight:650;word-break:break-word}.pallet-num{font-weight:800;font-variant-numeric:tabular-nums;color:#334d62}
.status{display:inline-flex;align-items:center;border-radius:50px;padding:5px 10px;font-size:12px;font-weight:750;background:#edf2f7;color:#48647b}
.status-accepted{background:#e6f5ed;color:#22805e}.status-processing{background:#fff2df;color:#956025}
.status-uncertain{background:#fde9e9;color:#b64b4b}
.action-links{display:flex;gap:7px;align-items:center;flex-wrap:wrap}
.action-links a{padding:6px 8px;background:#eff5f9;border-radius:7px;font-weight:650;font-size:12px}
.empty{text-align:center;color:var(--muted);padding:32px 15px}
.paging{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-top:18px;font-size:13px;color:var(--muted);flex-wrap:wrap}
.pages{display:flex;gap:7px}.pages a,.pages span{padding:7px 10px;border:1px solid var(--line);border-radius:8px;background:#fff;color:#526b80}
.pages .disabled{opacity:.45}
.footer{color:var(--muted);font-size:12px;margin:24px 2px}
@media(max-width:960px){.forms-grid{grid-template-columns:1fr}.kpis{grid-template-columns:repeat(2,minmax(0,1fr))}.shell{padding:14px}}
@media(max-width:620px){h1{font-size:23px}.topbar{align-items:flex-start;padding:13px}.brand b{font-size:16px}.account{gap:5px}.partner-pill{font-size:11px;padding:7px}.account .btn{font-size:12px;padding:7px;min-height:36px}
  .kpi{padding:11px}.kpi .number{font-size:24px}.fields-inline{grid-template-columns:1fr;gap:0}
  .panel,section{padding:16px;margin:12px 0}.forms-grid .panel{margin:0}
  table,tbody,tr,td{display:block;width:100%}thead{display:none}
  .table-wrap{overflow:visible}
  tbody{display:grid;gap:11px}
  tbody tr{border:1px solid var(--line);border-radius:11px;overflow:hidden;background:#fff!important;padding:8px 13px}
  tbody tr:hover{background:#f8fafc!important}
  td{border:0;padding:5px 0;display:flex;align-items:flex-start;gap:10px;justify-content:space-between}
  td:before{content:attr(data-label);color:#81909c;font-size:12px;min-width:75px}
  td .article{max-width:65%;text-align:right}.action-links{justify-content:flex-end}
  .toolbar .field{flex-basis:100%}.toolbar .status-filter{flex:1}
}
/* GRIT WMS design tokens. Partner portal uses the same dark surface as manager pages. */
:root{color-scheme:dark;--bg:#09121e;--paper:#142337;--ink:#f1f6ff;--muted:#9eb5ca;--navy:#2d92e2;--line:#30465c;--soft:#192e43;--blue:#203b52}
body{background:var(--bg);color:var(--ink)}
.topbar,section,.panel,.kpi{background:linear-gradient(145deg,#16283b,#101c2a);border-color:var(--line);box-shadow:none}
.logo{background:#176eae}
.brand b,h1,h2,h3,.article{color:#f1f6ff}
.partner-pill,.panel-icon,.dropzone,.btn-secondary,.btn-ghost,.pages a,.pages span,.status{background:#203b50;color:#d9eafa;border-color:#375a75}
.kpi .number.blue{color:#7fc8ff}.kpi .number.green{color:#64d8a6}.kpi .number.amber{color:#ffba70}
label{color:#b9cde0}
input:not([type=hidden]),select{background:#0d1d2c;color:#ecf5ff;border-color:#42607a}
input::placeholder{color:#819bb1}
input[type=file]{background:#102235}
input:focus,select:focus{border-color:#ff9230;box-shadow:0 0 0 2px #ff923044}
.btn-primary{background:#176eae;color:#fff}
.btn-secondary,.btn-ghost{color:#e0f0ff}
.dropzone{background:#152a3d;border-color:#52728a}
.notice{background:#173e36;color:#81e9b9;border-color:#2c6a57}
.notice.error{background:#41282f;color:#ffb9bd;border-color:#874853}
th{color:#b8d7ed;border-color:#345068}
td{color:#e4eef8;border-color:#2b4255}
tbody tr:nth-child(odd){background:#15283a}
tbody tr:hover{background:#1e3549}
.pallet-num{color:#c9e4f7}
.status-accepted{background:#173e36;color:#81e9b9}
.status-processing{background:#493521;color:#ffcd85}
.status-uncertain{background:#41282f;color:#ffb9bd}
.action-links a{background:#203b50;color:#9fd3fb}
a{color:#77c0ff}
a:focus-visible,button:focus-visible,input:focus-visible,select:focus-visible{outline:2px solid #ff9230;outline-offset:2px}
@media(max-width:620px){
 tbody tr,tbody tr:hover{background:#14283b!important}
 td:before{color:#9eb5ca}
}
@media print{
 :root{color-scheme:light}
 html,body{background:#fff!important;color:#000!important}
}

"""
FORM_JS = """
<script>
document.querySelectorAll('form[data-once]').forEach(form=>{
  form.addEventListener('submit',event=>{
    if(form.dataset.submitting==='1'){event.preventDefault();return}
    if(!form.checkValidity())return;
    form.dataset.submitting='1';
    const submit=form.querySelector('button[type=submit]');
    if(submit){submit.textContent='Обрабатываем…';setTimeout(()=>{submit.disabled=true},0)}
  });
});
const chooser=document.querySelector('#partner-file');
const fileName=document.querySelector('#partner-file-name');
if(chooser&&fileName){
  chooser.addEventListener('change',()=>{
    fileName.textContent=chooser.files&&chooser.files.length ? chooser.files[0].name : 'Файл не выбран';
  });
}
</script>
"""

def page(title, body, show_title=True):
    header = '<h1>'+escape(title)+'</h1>' if show_title else ''
    return ('<!doctype html><html lang="ru"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>'+escape(title)+'</title><style>'+CSS+'</style></head>'
            '<body><main class="shell">'+header+body+'</main></body></html>')

def local_date(raw):
    try:
        dt=datetime.fromisoformat(str(raw).replace('Z','+00:00'))
        if dt.tzinfo is None: dt=dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone(timedelta(hours=3))).strftime('%d.%m.%Y %H:%M')
    except (ValueError,TypeError):
        return str(raw)[:16]

def status_html(status):
    value={'created':('Создана',''), 'accepted':('Принята','status-accepted'),
           'processing':('В обработке','status-processing'),
           'uncertain':('Требует сверки','status-uncertain')}.get(status,('Неизвестно',''))
    return '<span class="status '+value[1]+'">'+escape(value[0])+'</span>'

def dashboard_view(partner, token, stats, rows, labels, q, status, current_page, page_count, total, notice, catalog_widget, created_pallet="", created_count=0, shipments=(), batches=()):
    q_text=escape(q,quote=True)
    token=escape(token,quote=True)
    partner_text=escape(partner,quote=True)
    options=[('','Все статусы'),('created','Создана'),('processing','В обработке'),
             ('accepted','Принята'),('uncertain','Требует сверки')]
    opts=''.join('<option value="'+key+'"'+(' selected' if key==status else '')+'>'+label+'</option>'
                 for key,label in options)
    message={'created':'Палета создана. QR доступен в истории.',
             'uploaded':'Файл обработан, палеты добавлены в историю.',
             'duplicate':'Эта отправка уже обработана, повторные палеты не создавались.'}.get(notice,'')
    if notice=='uploaded' and created_count>0:
        message='Excel обработан: создано палет '+str(created_count)+'.'
    print_link=('<a href="/partners/pallet/'+escape(created_pallet,quote=True)+'/print">Распечатать QR →</a>'
                if notice=='created' and created_pallet else '')
    flash='<div class="notice" role="status">'+escape(message)+' '+print_link+'</div>' if message else ''
    table_rows=[]
    for pid, code, ref, created_at, state in rows:
        label=labels.get(pid,{})
        sku=escape(label.get('article','Неизвестный артикул'))
        ordinal=escape(label.get('number','1/1'))
        actions='<a href="/partners/pallet/'+escape(pid,quote=True)+'/print">QR / Печать</a>'
        if state=='created':
            actions+=' <a href="/partners/pallet/'+escape(pid,quote=True)+'/edit">Изменить</a>'
        table_rows.append('<tr><td data-label="Артикул"><span class="article">'+sku+'</span></td>'
                          '<td data-label="Палета"><span class="pallet-num">'+ordinal+'</span></td>'
                          '<td data-label="Статус">'+status_html(state)+'</td>'
                          '<td data-label="Создана">'+escape(local_date(created_at))+'</td>'
                          '<td data-label="Действия"><div class="action-links">'+actions+'</div></td></tr>')
    if not table_rows:
        table_rows=['<tr><td colspan="5" class="empty">Палеты не найдены. Попробуй изменить фильтры или загрузить Excel.</td></tr>']
    def page_url(n):
        return '/partners?'+urlencode({'q':q,'status':status,'page':n})
    back='<a href="'+escape(page_url(current_page-1),quote=True)+'">← Назад</a>' if current_page>1 else '<span class="disabled">← Назад</span>'
    forward='<a href="'+escape(page_url(current_page+1),quote=True)+'">Дальше →</a>' if current_page<page_count else '<span class="disabled">Дальше →</span>'
    page_nav='<div class="paging"><span>Найдено: '+str(total)+' · Страница '+str(current_page)+' из '+str(page_count)+'</span><div class="pages">'+back+forward+'</div></div>'
    batch_cards=''.join(
        '<div class="panel" style="margin:8px 0;padding:14px">'
        '<b>'+escape(str(filename))+'</b> · '+str(count)+' палет, '+str(int(created or 0))+
        ' ожидают приёмки <span class="muted">('+escape(local_date(when))+')</span>'
        ' <a class="btn btn-secondary" href="/partners/uploads/'+escape(str(bid),quote=True)+'">Открыть / печать QR</a></div>'
        for bid,filename,when,count,created in batches)
    html=f"""
    <header class="topbar">
      <div class="brand"><span class="logo">ONX</span><div><b>ONIX WMS</b><small>Кабинет партнёра</small></div></div>
      <div class="account"><span class="partner-pill">Партнёр: {partner_text}</span>
      <form method="post" action="/partners/logout"><input type="hidden" name="csrf" value="{token}">
      <button class="btn btn-secondary" type="submit">Выйти</button></form></div>
    </header>
    <div class="dashboard-title"><h1>Палеты партнёра</h1>
      <div class="muted">Загрузи полученный от компании Excel, распечатай QR и при необходимости измени состав палет.</div></div>
    {flash}
    <div class="kpis">
      <div class="kpi"><div class="kpi-label">Всего палет</div><div class="number">{stats['total']}</div></div>
      <div class="kpi"><div class="kpi-label">Ожидают приёмки</div><div class="number blue">{stats['created']}</div></div>
      <div class="kpi"><div class="kpi-label">Принято</div><div class="number green">{stats['accepted']}</div></div>
      <div class="kpi"><div class="kpi-label">В работе / сверке</div><div class="number amber">{stats['pending']}</div></div>
    </div>
    <div class="forms-grid">
      <section class="panel" style="grid-column:1/-1">
        <div class="panel-top"><span class="panel-icon">📄</span><div><h2>1. Загрузить Excel</h2>
          <div class="muted">Файл заказа, который прислала наша компания</div></div></div>
        <form method="post" action="/partners/upload" enctype="multipart/form-data" data-once>
          <input type="hidden" name="csrf" value="{token}">
          <input type="hidden" name="submission_key" value="{escape(__import__('secrets').token_urlsafe(24),quote=True)}">
          <div class="dropzone"><div class="symbol">📁</div><b>Выбери Excel-файл</b>
            <div class="muted">.xlsx · не более 2 МБ · до 5000 палет</div>
            <input id="partner-file" type="file" name="file" accept=".xlsx" aria-label="Загрузить Excel" required>
            <div class="filename" id="partner-file-name">Файл не выбран</div></div>
          <div class="field-note">Загрузка создаёт палеты и QR. Затем откроется список с массовой печатью и корректировками.</div>
          <div class="form-foot"><button class="btn btn-primary" type="submit">Создать палеты из Excel</button></div>
        </form>
      </section>
    </div>
    <section class="panel">
      <div class="panel-top"><span class="panel-icon">📚</span><div><h2>2. Загруженные Excel-файлы</h2>
        <div class="muted">Открой файл, распечатай все QR или выбери палету для корректировки</div></div></div>
      {batch_cards or '<p class="muted">Загрузок пока нет.</p>'}
    </section>
    <section class="panel">
      <div class="panel-top"><span class="panel-icon">🗂️</span><div><h2>История палет</h2><div class="muted">Поиск, статусы и повторная печать QR</div></div></div>
      <form method="get" action="/partners" class="toolbar">
        <div class="field"><label for="history-search">Поиск по артикулу</label>
          <input id="history-search" name="q" value="{q_text}" maxlength="100" placeholder="Введите артикул" autocomplete="off"></div>
        <div class="field status-filter"><label for="history-status">Статус</label><select id="history-status" name="status">{opts}</select></div>
        <button class="btn btn-primary" type="submit">Найти</button>
        <a class="btn btn-ghost" href="/partners">Сбросить</a>
      </form>
      <div class="table-wrap"><table><thead><tr><th>Артикул</th><th>Палета</th><th>Статус</th><th>Создана (МСК)</th><th>Действия</th></tr></thead>
      <tbody>{''.join(table_rows)}</tbody></table></div>
      {page_nav}
    </section><div class="footer">ONIX WMS · Партнёр видит только свои палеты и может изменять только не принятые.</div>
    {FORM_JS}
    """
    return page("ONIX WMS · Палеты партнёра",html,show_title=False)


def edit_view(token, article, quantity, history_rows, widget, back="", back_kind="shipment", line_id="", line_options=(), pallet_id=""):
    """Restricted partner edit page with the same design as the main portal."""
    history=[]
    for before, oldqty, after, newqty, reason, changed_at in history_rows:
        prev=escape((str(before or '')+' · '+str(oldqty or '')) if before else 'Создание палеты')
        curr=escape(str(after or '')+' · '+str(newqty or ''))
        history.append('<li style="padding:10px 0;border-bottom:1px solid var(--line)">'
            '<small class="muted">'+escape(local_date(changed_at))+'</small><br>'
            '<b>'+prev+'</b> → <b>'+curr+'</b><br>'
            '<span class="muted">'+escape(str(reason or ''))+'</span></li>')
    back_path=('/partners/uploads/' if back_kind=='upload' else '/partners/shipments/')+escape(back,quote=True) if back else '/partners'
    # Explicitly choose a line when a pallet contains multiple SKU records.
    line_choices=''
    if len(line_options)>1:
        parts=[]
        for lid, sku, qty in line_options:
            url='/partners/pallet/'+escape(pallet_id,quote=True)+'/edit?line_id='+escape(lid,quote=True)
            if back:
                url+='&back='+escape(back,quote=True)
            parts.append('<a class="btn '+('btn-primary' if lid==line_id else 'btn-secondary')+
                         '" href="'+url+'">'+escape(str(sku))+' · '+str(qty)+' шт.</a>')
        line_choices='<p class="muted">Выбери позицию этой палеты для замены:</p><div class="action-links">'+''.join(parts)+'</div>'
    inner=('<div class="topbar"><div class="brand"><span class="logo">ONX</span>'
        '<div><b>ONIX WMS</b><small>Корректировка палеты</small></div></div>'
        '<a class="btn btn-ghost" href="'+back_path+'">← Назад</a></div>'
        '<div class="dashboard-title"><h1>Изменить палету</h1>'
        '<div class="muted">Редактировать можно только до начала приёмки. QR сохранится прежним.</div></div>'
        '<section class="panel" style="max-width:700px"><h2>Состав палеты</h2>'+line_choices+
        '<form method="post" data-once><input type="hidden" name="csrf" value="'+escape(token,quote=True)+'">'+
        '<input type="hidden" name="back" value="'+escape(back,quote=True)+'">'+
        '<input type="hidden" name="line_id" value="'+escape(line_id,quote=True)+'">'
        '<div class="field"><label for="edit-article">Артикул товара</label>'
        '<input id="edit-article" name="article" maxlength="150" required value="'+escape(article,quote=True)+'"></div>'
        '<div class="fields-inline"><div class="field"><label for="edit-qty">Количество, шт.</label>'
        '<input id="edit-qty" type="number" name="quantity" min="1" max="1000000" step="1" required value="'+escape(str(quantity),quote=True)+'"></div>'
        '<div class="field"><label for="edit-reason">Причина корректировки</label>'
        '<input id="edit-reason" name="reason" required maxlength="300" placeholder="Что изменилось при погрузке?"></div></div>'
        +widget+'<button type="submit" class="btn btn-primary">Сохранить изменения</button></form></section>'
        '<section class="panel" style="max-width:700px"><h2>История корректировок</h2><ul style="list-style:none;padding:0;margin:0">'
        +(''.join(history) if history else '<li class="muted">Пока изменений не было</li>')
        +'</ul></section>'+FORM_JS)
    return page("ONIX WMS · Изменить палету",inner,show_title=False)


def shipment_list_view(partner, token, shipments):
    token=escape(token,quote=True)
    rows=''.join(
        '<div class="panel"><b>Поставка на '+escape(str(day))+'</b> · '+str(count)+' палет'
        ' <a class="btn btn-secondary" href="/partners/shipments/'+escape(str(sid),quote=True)+'">Открыть</a></div>'
        for sid,day,count in shipments)
    return page("ONIX · Поставки",'''
      <div class="dashboard-title"><a href="/partners">← Палеты партнёра</a><h1>Поставки</h1>
      <p class="muted">Каждая поставка объединяет несколько палет. Печать всех QR одним заданием А4.</p></div>
      <section class="panel"><h2>Новая поставка</h2>
      <form method="post" action="/partners/shipments/new" data-once>
      <input type="hidden" name="csrf" value="'''+token+'''">
      <div class="field"><label>Дата поставки</label><input type="date" name="planned_date" required></div>
      <div class="form-foot"><button class="btn btn-primary" type="submit">Создать поставку</button></div>
      </form></section>'''+(rows or '<div class="panel">Пока нет поставок</div>')+FORM_JS)


def shipment_detail_view(token, day, shipment_id, rows, labels, widget):
    safe_id=escape(shipment_id,quote=True)
    table=[]
    for pid, barcode, status, created, article in rows:
        d=labels.get(pid,{})
        actions='<a href="/partners/pallet/'+escape(pid,quote=True)+'/print">QR / Печать</a>'
        if status=='created':
            actions+=' <a href="/partners/pallet/'+escape(pid,quote=True)+'/edit?back='+safe_id+'">Изменить</a>'
        table.append('<tr><td>'+escape(d.get("article") or article)+'</td><td>'+
          escape(d.get("number") or "1/1")+'</td><td>'+status_html(status)+'</td><td class="action-links">'+actions+'</td></tr>')
    safe_token=escape(token,quote=True)
    return page("ONIX · Поставка",f'''
    <div class="dashboard-title"><a href="/partners/shipments">← Все поставки</a>
    <h1>Поставка на {escape(str(day))}</h1><p class="muted">Палет: {len(rows)}</p></div>
    <section class="panel">
    <a class="btn btn-primary" target="_blank" rel="noopener" href="/partners/shipments/{safe_id}/print">🖨 Печать всех палет А4</a>
    <a class="btn btn-secondary" target="_blank" rel="noopener" href="/partners/shipments/{safe_id}/print?only=created">Только ожидающие</a>
    </section>
    <div class="forms-grid">
    <section class="panel"><h2>Добавить палету</h2>
    <form action="/partners/pallet/new" method="post" data-once>
    <input type="hidden" name="csrf" value="{safe_token}">
    <input type="hidden" name="shipment_id" value="{safe_id}">
    <div class="field"><label>Артикул</label><input name="article" required maxlength="150" placeholder="Артикул или название"></div>
    <div class="field"><label>Количество, шт.</label><input name="quantity" type="number" min="1" max="1000000" required></div>
    <div class="field"><label>Причина</label><input name="reason" required maxlength="300" value="Дополнение поставки"></div>
    {widget}<div class="form-foot"><button class="btn btn-primary">Добавить палету</button></div></form></section>
    <section class="panel"><h2>Загрузить Excel</h2>
    <form action="/partners/upload" method="post" enctype="multipart/form-data" data-once>
    <input type="hidden" name="csrf" value="{safe_token}">
    <input type="hidden" name="shipment_id" value="{safe_id}">
    <div class="field"><label>Excel-файл</label><input type="file" name="file" accept=".xlsx" required></div>
    <div class="form-foot"><button class="btn btn-primary">Добавить из Excel</button></div></form></section>
    </div>
    <section class="panel"><h2>Палеты поставки</h2>
    <div class="table-wrap"><table><thead><tr><th>Артикул</th><th>Палета</th><th>Статус</th><th>Действия</th></tr></thead>
    <tbody>{''.join(table) or '<tr><td colspan="4">Пока нет палет</td></tr>'}</tbody></table></div></section>
    {FORM_JS}
    ''')


def upload_batch_view(batch, rows, labels):
    """Existing Excel upload. Partner can print its QR batch without creating a shipment."""
    bid, filename, created_at = batch
    safe_id=escape(str(bid), quote=True)
    total=len(rows)
    pending=sum(1 for r in rows if r[2]=='created')
    buttons=[]
    for only, count, caption in (('all',total,'🖨 Печать всех QR А4'),
                                 ('created',pending,'Только ожидающие')):
        if count < 1:
            continue
        parts=(count+499)//500
        for part in range(1,parts+1):
            text=caption if parts==1 else caption+' · часть '+str(part)+'/'+str(parts)
            buttons.append('<a class="btn btn-primary" target="_blank" rel="noopener" href="/partners/uploads/'+
                safe_id+'/print?only='+only+'&part='+str(part)+'">'+text+'</a>')
    table=[]
    for pid, barcode, state, when, fallback in rows:
        display=labels.get(pid,{})
        article=escape(display.get('article') or fallback or 'Не указан')
        number=escape(display.get('number') or '1/1')
        qty=display.get('quantity')
        qty_text=(str(qty)+' шт.') if qty is not None else 'Нет количества'
        actions='<a href="/partners/pallet/'+escape(pid,quote=True)+'/print" target="_blank" rel="noopener">QR / Печать</a>'
        if state=='created':
            actions+=' <a href="/partners/pallet/'+escape(pid,quote=True)+'/edit?back='+safe_id+'">Изменить</a>'
        table.append('<tr><td>'+article+'</td><td>'+number+'</td><td>'+escape(qty_text)+
            '</td><td>'+status_html(state)+'</td><td class="action-links">'+actions+'</td></tr>')
    return page('ONIX · Палеты из Excel', '''
    <div class="dashboard-title"><a href="/partners">← Все Excel-файлы</a>
      <h1>'''+escape(str(filename))+'''</h1>
      <p class="muted">Загружено: '''+escape(local_date(created_at))+''' · Палет: '''+
      str(total)+''' · Ожидают приёмки: '''+str(pending)+'''</p></div>
    <section class="panel">
      <div class="panel-top"><span class="panel-icon">🖨</span><div><h2>Массовая печать QR</h2>
      <div class="muted">Одна палета — один лист А4. Если состав палеты изменён, QR остаётся прежним.</div></div></div>
      <div class="action-links">'''+(' '.join(buttons) if buttons else '<span class="muted">Нет палет для печати</span>')+'''</div>
    </section>
    <section class="panel"><h2>Палеты из файла</h2>
      <div class="table-wrap"><table><thead><tr><th>Артикул</th><th>Палета</th>
      <th>Количество</th><th>Статус</th><th>Действия</th></tr></thead>
      <tbody>'''+''.join(table)+'''</tbody></table></div>
      <p class="muted">Корректировать можно только палеты, не начатые в приёмке.</p>
    </section>''')
