"""Opt-in wiring to EXISTING GRIT app and WMS session; NOT a new demo app.

Production remains completely off. The original WB print flow can run only
with a disposable PG17, synthetic identity and explicit isolated test switch.
Original PDF print mode cannot be silently activated on an employer warehouse.
"""
import os
import re


def register_pr211_isolated(app):
    flag=os.getenv('GRIT_PR211_ORIGINAL_FLOW','').strip()
    if not flag:
        return False
    if flag!='YES_ISOLATED_SYNTHETIC':
        raise RuntimeError('UNRECOGNIZED_PR211_PRINT_MODE')
    if os.getenv('GRIT_SYNTHETIC_PG_TEST')!='YES_ISOLATED':
        raise RuntimeError('PR211_SYNTHETIC_DATABASE_GATE_REQUIRED')
    url=os.getenv('PR211_SYNTHETIC_DATABASE_URL','')
    if not re.fullmatch(
        r'postgresql://postgres:synthetic-only@pr211-db:5432/grit_pr211_synthetic_[a-z0-9_]+',
        url):
        raise RuntimeError('PR211_DISPOSABLE_DATABASE_REQUIRED')
    keyhex=os.getenv('PR211_TEST_AES_KEY_HEX','')
    if not re.fullmatch(r'[0-9a-fA-F]{64}',keyhex):
        raise RuntimeError('PR211_EPHEMERAL_AES_KEY_REQUIRED')
    import psycopg
    from services.printing.wb_original_pg17_outbox import initialize as init_queue
    from services.printing.wb_original_pg17_device_auth import initialize as init_devices
    from services.printing.wb_original_print_http_synthetic import (
        make_isolated_original_print_blueprint,
        verified_wms_manager_guard,verified_wms_picking_guard,
    )

    def connection_factory():
        return psycopg.connect(url,autocommit=True)

    with connection_factory() as db:
        init_queue(db)
        init_devices(db)

    app.register_blueprint(make_isolated_original_print_blueprint(
        connection_factory,key=bytes.fromhex(keyhex),
        manager_guard=verified_wms_manager_guard,
        worker_guard=verified_wms_picking_guard))

    # Reuse the *existing* /fbs and manager PDF screens. Never create a
    # competing picking page or an independent warehouse workflow.
    from flask import request

    worker_panel = r"""<section id="pr211-ui" style="background:#101e30;color:#e9f1fa;
      border:1px solid #304c67;border-radius:12px;padding:14px;margin:16px 0">
      <strong>GRIT · Принтер по линиям</strong>
      <p style="color:#a2b7cb">Изолированная проверка. Только синтетические задания.</p>
      <label for="pr211-qr">QR закреплённого принтера</label>
      <input id="pr211-qr" autocomplete="off" spellcheck="false"
       style="display:block;width:100%;max-width:500px;background:#0a1524;
       border:1px solid #47617a;color:white;padding:11px;border-radius:8px">
      <button id="pr211-go" type="button"
       style="margin-top:10px;background:#326ec7;color:white;padding:10px 14px;
       border:0;border-radius:8px">Подтвердить принтер</button>
      <p id="pr211-status" role="status" aria-live="polite"></p>
      <script>(function(){
        const field=document.getElementById('pr211-qr'),
              button=document.getElementById('pr211-go'),
              status=document.getElementById('pr211-status');
        async function run(){
          if(button.disabled)return;
          button.disabled=true;status.textContent='Проверка...';
          try{
            const first=await fetch('/synthetic/pr211/worker/session',
              {credentials:'same-origin',cache:'no-store'});
            const info=await first.json();
            if(!first.ok)throw Error(info.error||'Нет активной сборки');
            const response=await fetch('/synthetic/pr211/worker/printer-qr',{
              method:'POST',credentials:'same-origin',
              headers:{'Content-Type':'application/json'},
              body:JSON.stringify({csrf:info.csrf,qr:field.value.trim()})});
            const result=await response.json();
            if(!response.ok)throw Error(result.error||'QR отклонён');
            status.textContent=result.printer+' · Линии: '+result.lines.join(', ')+
              ' · Передано в очередь: '+result.released;
            field.value='';
          }catch(error){status.textContent='Ошибка: '+error.message}
          finally{button.disabled=false;field.focus()}
        }
        field.addEventListener('keydown',e=>{if(e.key==='Enter'){e.preventDefault();run()}});
        button.addEventListener('click',run);
      })();</script></section>"""

    manager_panel = r"""<section id="pr211-ui" style="background:#101e30;color:#e9f1fa;
       border:1px solid #304c67;border-radius:12px;padding:15px;margin:18px 0">
       <h3>GRIT · Оригинальные стикеры WB · Изолированный тест</h3>
       <p style="color:#afc1d0">PDF сохраняется как зашифрованные проверенные
       страницы. Сначала загрузи файл, затем сотрудник сканирует QR принтера в /fbs.</p>
       <form id="pr211-form">
         <label>Принтер <input name="printer_id" required maxlength="40"
           placeholder="ID принтера" style="background:#071424;color:white;padding:9px"></label>
         <label>Оригинальный PDF <input type="file" name="file" accept=".pdf,application/pdf" required></label>
         <button type="submit" style="background:#326ec7;color:white;padding:10px;border:0;border-radius:8px">
         Проверить и поставить в ожидание QR</button>
       </form>
       <button type="button" id="pr211-overview"
         style="background:#30465e;color:white;padding:9px;margin-top:12px;border:0;border-radius:8px">
         Очередь и устройства</button>
       <pre id="pr211-manager-status" style="background:#081624;color:#dceafa;
         border-radius:8px;padding:10px;white-space:pre-wrap;overflow-wrap:anywhere"></pre>
       <script>(function(){
         const form=document.getElementById('pr211-form'),
               output=document.getElementById('pr211-manager-status'),
               overview=document.getElementById('pr211-overview');
         form.addEventListener('submit',async e=>{
           e.preventDefault();output.textContent='Проверка PDF...';
           try{
             const session=await fetch('/synthetic/pr211/manager/session',
               {credentials:'same-origin',cache:'no-store'}),auth=await session.json();
             if(!session.ok)throw Error(auth.error||'Требуется руководитель');
             const data=new FormData(form);data.append('csrf',auth.csrf);
             const r=await fetch('/synthetic/pr211/manager/pdf/stage',{
               method:'POST',body:data,credentials:'same-origin'});
             const j=await r.json();if(!r.ok)throw Error(j.error||'PDF не принят');
             output.textContent='Ожидают QR: '+j.staged+' · '+JSON.stringify(j.states);
           }catch(err){output.textContent='Отказ: '+err.message}
         });
         overview.addEventListener('click',async()=>{
           try{
             const r=await fetch('/synthetic/pr211/manager/overview',{
               credentials:'same-origin',cache:'no-store'});
             const j=await r.json();if(!r.ok)throw Error(j.error||'Недоступно');
             output.textContent=JSON.stringify(j,null,2);
           }catch(err){output.textContent='Отказ: '+err.message}
         });
       })();</script></section>"""

    @app.after_request
    def isolated_pr211_print_controls(response):
        if (request.path not in ('/fbs','/manager/fbs-pdf')
                or response.status_code!=200
                or not response.mimetype.startswith('text/html')):
            return response
        html=response.get_data(as_text=True)
        if '</body>' not in html or 'id="pr211-ui"' in html:
            return response
        panel=worker_panel if request.path=='/fbs' else manager_panel
        response.set_data(html.replace('</body>',panel+'</body>',1))
        response.headers['Cache-Control']='no-store'
        return response

    return True
