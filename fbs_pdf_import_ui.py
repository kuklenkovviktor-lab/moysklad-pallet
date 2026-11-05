"""Manager interface for the first real PDF intake / format discovery."""

FBS_PDF_PAGE = """<!doctype html><html lang="ru"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>GRIT WMS · Загрузка PDF FBS</title>
<style>
*{box-sizing:border-box}
body{margin:0;background:#e9eef3;color:#25313d;font:14px system-ui,-apple-system,Segoe UI,sans-serif}
main{max-width:1200px;margin:auto;padding:18px}
h1{font-size:27px;margin:10px 0 4px}h2{font-size:19px;margin:0 0 12px}
a{color:#365f79}p{line-height:1.5}
.card{border:1px solid #d6e0e7;border-radius:15px;background:#f9fbfd;padding:18px;margin:14px 0}
.small{color:#617584;font-size:12px}
label{font-weight:750;display:block;margin:12px 0}
input[type=file]{display:block;margin-top:8px;max-width:100%;background:white;border:1px solid #c7d4df;border-radius:9px;padding:12px;width:100%}
button{background:#48647a;border:0;color:#fff;font-weight:800;padding:12px 18px;border-radius:10px;cursor:pointer}
button:disabled{opacity:.5;cursor:wait}
#message{margin-top:12px;min-height:25px;font-weight:600}
.error{color:#a14141}.good{color:#246948}
.notice{border-left:4px solid #a88652;background:#fff8e8;padding:11px 13px;border-radius:7px;margin:12px 0}
.summary{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:9px}
.metric{background:#edf3f6;border-radius:10px;padding:12px}
.metric .v{font-weight:850;font-size:25px;margin-top:5px;overflow-wrap:anywhere}
.metric .l{font-size:12px;color:#627485}
#summary[hidden],#results[hidden],#importPdf[hidden]{display:none}
details{border:1px solid #dce5ec;border-radius:10px;background:#fff;margin:10px 0;overflow:hidden}
summary{padding:13px;cursor:pointer;font-weight:750}
.sample{padding:0 13px 14px}
pre{white-space:pre-wrap;overflow-wrap:anywhere;font:12px/1.6 ui-monospace,Consolas,monospace;color:#26394a;background:#eef3f6;padding:12px;border-radius:8px;max-height:280px;overflow:auto}
.tablewrap{overflow:auto;max-height:650px;border:1px solid #dce5ec;border-radius:9px}
table{border-collapse:collapse;min-width:940px;width:100%;font-size:12px;background:white}
th,td{padding:9px;border-bottom:1px solid #e1e8ed;text-align:left;vertical-align:top}
th{position:sticky;top:0;background:#eaf0f4;z-index:1}
.pass{color:#206744;font-weight:800}.fail{color:#a23c3c;font-weight:800}
.hint{color:#8c611b;font-size:12px}.printcode{font:11px ui-monospace,Consolas,monospace}
.chip{display:inline-block;background:#e0ebf1;border-radius:8px;font-size:12px;padding:6px 8px;margin:3px}
ul{padding-left:20px}li{line-height:1.5;margin:6px 0}
@media(max-width:640px){.summary{grid-template-columns:repeat(2,minmax(0,1fr))}main{padding:10px}}

/* PDF-specific dark surfaces. Shared grit-ui.css is inserted AFTER this style,
   so these scoped selectors override both the legacy light mode and shared theme. */
#grit-pdf-workspace .tablewrap{
 background:#102033!important;border:1px solid #36516a!important;border-radius:11px!important
}
#grit-pdf-workspace details{
 background:#14283b!important;color:#ecf5ff!important;
 border:1px solid #39566f!important;border-radius:10px!important;
 box-shadow:none!important
}
#grit-pdf-workspace details>summary{
 background:#20384f!important;color:#eaf5ff!important;
 border-bottom:1px solid transparent!important;
 padding:12px 13px!important;line-height:1.45;
 outline-offset:-2px
}
#grit-pdf-workspace details[open]>summary{
 background:#24435e!important;border-bottom-color:#3a617d!important
}
#grit-pdf-workspace details>summary::marker{color:#8ed0ff}
#grit-pdf-workspace details>summary:hover{background:#2b4c69!important}
#grit-pdf-workspace details .sample{
 background:#122638!important;color:#e3f1fd!important;
 padding:13px!important;overflow-wrap:anywhere
}
#grit-pdf-workspace details .printcode{
 display:block;padding:7px 0;color:#e3f4ff!important;
 border-bottom:1px solid #304a60;overflow-wrap:anywhere
}
#grit-pdf-workspace pre{
 background:#1d3449!important;color:#f1f7ff!important;
 border:1px solid #38566c!important;font-size:12px;line-height:1.65
}
#grit-pdf-workspace .notice{
 background:#192c40!important;color:#dcecff!important;
 border-left:3px solid #4a9dd8!important
}
#grit-pdf-workspace .notice.good{
 background:#132e2b!important;color:#a1f3ca!important;
 border-left-color:#3fcc91!important
}
#grit-pdf-workspace .notice.error{
 background:#36242a!important;color:#ffb1b5!important;
 border-left-color:#ff767e!important
}
#grit-pdf-workspace .metric{
 background:#1a3045!important;color:#eef7ff!important;
 border:1px solid #34536c!important
}
#grit-pdf-workspace .metric .l{color:#b2cfe5!important}
#grit-pdf-workspace .metric .v{color:#fff!important}
#grit-pdf-workspace .pass{color:#71e2a8!important}
#grit-pdf-workspace .fail{color:#ff9d9e!important}
#grit-pdf-workspace .chip{background:#25445c!important;color:#e8f5ff!important}
#grit-pdf-workspace .hint{color:#ffcf83!important}
#grit-pdf-workspace #wbProducts td:last-child{min-width:142px}
#grit-pdf-workspace #wbProducts td:last-child details{margin:0;min-width:122px}
#grit-pdf-workspace #wbProducts td:last-child details>summary{
 font-size:12px;padding:9px 11px!important
}
#grit-pdf-workspace #pages details>summary{font-size:13px}
#grit-pdf-workspace .tablewrap td,
#grit-pdf-workspace .tablewrap th{line-height:1.45}
@media(max-width:640px){
 #grit-pdf-workspace .card{padding:13px}
 #grit-pdf-workspace .tablewrap{max-height:480px}
 #grit-pdf-workspace details>summary{padding:12px!important}
}
</style></head><body><main id="grit-pdf-workspace">
<a href="/manager">← Менеджерская панель</a>
<h1>📄 Загрузка PDF FBS</h1>
<p class="small">Сначала проверь PDF, затем отдельно подтверди сохранение связок ШК WB с товарами. Никаких заданий на сборку и пополнение автоматически не создаётся.</p>
<section class="card">
<h2>Загрузить PDF с этикетками/заказами</h2>
<form id="form">
<label for="pdf">Выбери PDF
<input type="file" id="pdf" accept=".pdf,application/pdf" required></label>
<p class="small">Максимум 25 МБ · до 10 000 страниц. Сам PDF не хранится. После подтверждения в базу записываются только связки заказов и товаров.</p>
<button type="submit" id="submit">Проверить файл</button>
<button type="button" id="importPdf" hidden>Сохранить заказы для сборки PDF</button>
<div class="notice" id="pdfRegistryStatus" aria-live="polite">Проверяю реестр PDF…</div>
</form><div id="message" role="status"></div>
</section>
<section class="card" id="summary" hidden>
<h2>Что GRIT прочитала</h2>
<div class="summary" id="metrics"></div>
<p class="small" id="fileInfo"></p>
<div id="hints"></div>
</section>
<section class="card" id="wb" hidden>
<h2>WB FBS · Проверка всех стикеров</h2>
<p id="wbStatus" class="notice"></p>
<div class="summary" id="wbMetrics"></div>
<p class="small" id="wbRule"></p>
<div id="wbErrors"></div>
<h2>Артикулы и закреплённые ячейки</h2>
<div class="tablewrap"><table>
<thead><tr><th>Товарный листок, стр.</th><th>Артикул</th><th>ШК товара</th><th>Заявлено</th><th>Стикеров WB</th><th>Проверка</th><th>Ячейки</th><th>Стикеры</th></tr></thead>
<tbody id="wbProducts"></tbody></table></div>
<p class="small">Точные адреса берутся из карты GRIT. Если SKU закреплён за несколькими ячейками, выбрать адрес автоматически нельзя. Данные остаются только в ответе текущей загрузки.</p>
</section>
<section class="card" id="results" hidden>
<h2>Предпросмотр содержимого</h2>
<p class="small">GRIT показывает текст и размеры выбранных страниц. Найденные числа и артикулы пока не считаются подтверждёнными заказами. Для сканированных этикеток без текстового слоя понадобится отдельный этап распознавания.</p>
<div id="warnings"></div>
<div id="pages"></div>
</section>
</main><script>
const form=document.getElementById('form'),button=document.getElementById('submit');
const msg=document.getElementById('message'),summary=document.getElementById('summary');
const results=document.getElementById('results'),wbEl=document.getElementById('wb');
function esc(v){return String(v==null?'':v).replace(/[&<>"']/g,ch=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]))}
function metric(label,value){return '<div class="metric"><div class="l">'+esc(label)+'</div><div class="v">'+esc(value)+'</div></div>'}
function renderWB(d){
 const w=d.wb_report;
 if(!w){wbEl.hidden=true;return}
 wbEl.hidden=false;
 const good=w.valid_structure;
 document.getElementById('wbStatus').className='notice'+(good?' good':' error');
 document.getElementById('wbStatus').textContent=good?
  '✓ Весь файл согласован: количество уникальных ШК WB равно заявленным количествам по SKU. Это ещё не подтверждение от WB API.':
  '⚠ В документе есть ошибки структуры или количества! Автоматические действия запрещены.';
 document.getElementById('wbMetrics').innerHTML=[
  metric('Проверено страниц',w.pages_checked),
  metric('Стикеров WB',w.sticker_pages),
  metric('Артикулов',w.unique_articles),
  metric('Повторов листка',w.repeat_product_sheets),
  metric('Ячейка найдена',w.locations_available?w.one_location_articles:'н/д'),
  metric('Без адреса',w.locations_available?w.without_location_articles:'н/д'),
  metric('Несколько адресов',w.locations_available?w.multi_location_articles:'н/д'),
  metric('Структурных ошибок',w.errors.length)
 ].join('');
 document.getElementById('wbRule').textContent=w.counting_rule+' '+w.barcode_verification;
 const errors=(w.errors||[]).map(e=>'<div class="notice error">Стр. '+e.page+': '+esc(e.message)+'</div>').join('');
 document.getElementById('wbErrors').innerHTML=
  (w.locations_error?'<div class="notice">'+esc(w.locations_error)+'</div>':'')+errors;
 document.getElementById('wbProducts').innerHTML=(w.products||[]).map(p=>{
  const sheet=p.sheet_pages.join(', ');
  const locations=w.locations_available?(p.locations.length?p.locations.join(', '):'Не привязан'):'Нет доступа к карте';
  const stickers=(p.stickers||[]).map(x=>'<div class="printcode">'+x.page+': '+esc(x.barcode)+'</div>').join('');
  return '<tr><td>'+esc(sheet)+'</td><td><b>'+esc(p.article)+'</b></td>'+
   '<td class="printcode">'+esc(p.product_barcode)+'</td>'+
   '<td>'+p.declared_qty+'</td><td>'+p.sticker_count+'</td>'+
   '<td class="'+(p.quantity_matches?'pass':'fail')+'">'+(p.quantity_matches?'✓':'Ошибка')+'</td>'+
   '<td>'+esc(locations)+'</td>'+
   '<td><details><summary>Показать ('+p.sticker_count+')</summary><div class="sample">'+stickers+'</div></details></td></tr>';
 }).join('');
}
function render(d){
 const sampled=d.sampled_pages||0;renderWB(d);
 document.getElementById('metrics').innerHTML=[
  metric('Страниц PDF',d.pages),metric('Текст: выборка',sampled),
  metric('Текст найден',d.text_pages_in_sample),metric('Создано заданий',d.tasks_created)
 ].join('');
 document.getElementById('fileInfo').textContent='Файл: '+d.filename+' · '+(d.file_bytes/1048576).toFixed(2)+
  ' МБ · SHA256: '+d.sha256+' · режим: только анализ PDF';
 const hint=d.article_hints||[];
 document.getElementById('hints').innerHTML=hint.length?
  '<p><b>Предположительные артикулы из текста:</b> '+hint.map(v=>'<span class="chip">'+esc(v)+'</span>').join('')+'</p>':
  '<p class="small">Явно подписанные артикулы в проверенных страницах не обнаружены.</p>';
 document.getElementById('warnings').innerHTML=(d.warnings||[]).map(w=>
  '<div class="notice">'+esc(w)+'</div>').join('');
 document.getElementById('pages').innerHTML=(d.page_samples||[]).map((p,i)=>
  '<details '+(i===0?'open':'')+'><summary>Страница '+p.number+
  ' · '+p.width_mm+'×'+p.height_mm+' мм · '+(p.text_present?'текст есть':'текст не найден')+
  ' · изображений: '+p.images+'</summary><div class="sample">'+
  (p.excerpt?'<pre>'+esc(p.excerpt)+'</pre>':
  '<p class="small">Извлечь текст этой страницы не удалось.</p>')+
  '</div></details>').join('');
 document.getElementById('importPdf').hidden=!(d.wb_report && d.wb_report.valid_structure);
 summary.hidden=false;results.hidden=false;
}
async function loadPdfRegistryStatus(){
 try{
  const response=await fetch('/api/manager/fbs-pdf/status',{cache:'no-store'});
  if(!response.ok)return;
  const data=await response.json();
  document.getElementById('pdfRegistryStatus').textContent=
   'В реестре PDF: '+data.orders_total+' заказов. '+(data.orders_total>0?
     'Проверка соответствия товара и заказа выполняется автоматически при сборке.':
     'Сначала проверь и сохрани PDF. Пока заказов нет, работает обычная сборка.');
 }catch(e){}
}
document.getElementById('pdf').addEventListener('change',()=>{
 document.getElementById('importPdf').hidden=true;
});
document.getElementById('importPdf').addEventListener('click',async()=>{
 const file=document.getElementById('pdf').files[0];if(!file)return;
 const b=document.getElementById('importPdf');b.disabled=true;
 msg.className='';msg.textContent='Сохраняю подтверждённые связки PDF…';
 try{
  const data=new FormData();data.append('file',file);
  const response=await fetch('/api/manager/fbs-pdf/import',{method:'POST',body:data,cache:'no-store'});
  const result=await response.json();
  if(!response.ok)throw new Error(result.error||'Импорт отклонён');
  msg.className='good';msg.textContent='✓ В реестре PDF: '+result.orders_in_file+
   ' заказов из файла; новых '+result.orders_imported+
   ', уже существовали '+result.orders_existing+
   '. Контроль пересорта работает автоматически, без переключателей у сотрудников.';
  await loadPdfRegistryStatus();
 }catch(e){msg.className='error';msg.textContent=e.message}
 finally{b.disabled=false}
});
loadPdfRegistryStatus();
form.addEventListener('submit',async e=>{
 e.preventDefault();if(button.disabled)return;
 const file=document.getElementById('pdf').files[0];
 if(!file){msg.textContent='Выбери PDF';return}
 if(file.size>25*1024*1024){msg.className='error';msg.textContent='PDF больше 25 МБ';return}
 button.disabled=true;summary.hidden=true;results.hidden=true;wbEl.hidden=true;document.getElementById('importPdf').hidden=true;
 msg.className='';msg.textContent='Проверяю PDF…';
 try{
  const formData=new FormData();formData.append('file',file);
  const controller=new AbortController();
  const timer=setTimeout(()=>controller.abort(),110000);
  let r;try{r=await fetch('/api/manager/fbs-pdf/preview',{
   method:'POST',body:formData,cache:'no-store',signal:controller.signal
  })}finally{clearTimeout(timer)}
  if(r.status===403){location.href='/manager-login';return}
  const data=await r.json();
  if(!r.ok)throw new Error(data.error||'Не удалось прочитать PDF');
  render(data);msg.className='good';msg.textContent='✓ PDF проверен. Создание заданий отключено. Для сборки отдельно нажми «Сохранить заказы».';
 }catch(err){msg.className='error';msg.textContent=err.name==='AbortError'?
  'Обработка слишком долгая. Попробуй меньший файл для первого теста.':err.message;
 }finally{button.disabled=false}
});
</script></body></html>"""
