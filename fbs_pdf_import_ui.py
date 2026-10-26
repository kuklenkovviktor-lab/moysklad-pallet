"""Manager interface for the first real PDF intake / format discovery."""

FBS_PDF_PAGE = """<!doctype html><html lang="ru"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>ONIX WMS · Загрузка PDF FBS</title>
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
#summary[hidden],#results[hidden]{display:none}
details{border:1px solid #dce5ec;border-radius:10px;background:#fff;margin:10px 0;overflow:hidden}
summary{padding:13px;cursor:pointer;font-weight:750}
.sample{padding:0 13px 14px}
pre{white-space:pre-wrap;overflow-wrap:anywhere;font:12px/1.6 ui-monospace,Consolas,monospace;color:#26394a;background:#eef3f6;padding:12px;border-radius:8px;max-height:280px;overflow:auto}
.chip{display:inline-block;background:#e0ebf1;border-radius:8px;font-size:12px;padding:6px 8px;margin:3px}
ul{padding-left:20px}li{line-height:1.5;margin:6px 0}
@media(max-width:640px){.summary{grid-template-columns:repeat(2,minmax(0,1fr))}main{padding:10px}}
</style></head><body><main>
<a href="/manager">← Менеджерская панель</a>
<h1>📄 Загрузка PDF FBS</h1>
<p class="small">Первый этап: проверяем формат настоящего файла и способность ONIX извлекать его содержимое. Никаких заданий на сборку и пополнение автоматически не создаётся.</p>
<section class="card">
<h2>Загрузить PDF с этикетками/заказами</h2>
<form id="form">
<label for="pdf">Выбери PDF
<input type="file" id="pdf" accept=".pdf,application/pdf" required></label>
<p class="small">Максимум 25 МБ · до 10 000 страниц · файл не записывается в базу · информация показывается только тебе в этой вкладке.</p>
<button type="submit" id="submit">Проверить файл</button>
</form><div id="message" role="status"></div>
</section>
<section class="card" id="summary" hidden>
<h2>Что ONIX прочитал</h2>
<div class="summary" id="metrics"></div>
<p class="small" id="fileInfo"></p>
<div id="hints"></div>
</section>
<section class="card" id="results" hidden>
<h2>Предпросмотр содержимого</h2>
<p class="small">ONIX показывает текст и размеры выбранных страниц. Найденные числа и артикулы пока не считаются подтверждёнными заказами. Для сканированных этикеток без текстового слоя понадобится отдельный этап распознавания.</p>
<div id="warnings"></div>
<div id="pages"></div>
</section>
</main><script>
const form=document.getElementById('form'),button=document.getElementById('submit');
const msg=document.getElementById('message'),summary=document.getElementById('summary');
const results=document.getElementById('results');
function esc(v){return String(v==null?'':v).replace(/[&<>"']/g,ch=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]))}
function metric(label,value){return '<div class="metric"><div class="l">'+esc(label)+'</div><div class="v">'+esc(value)+'</div></div>'}
function render(d){
 const sampled=d.sampled_pages||0;
 document.getElementById('metrics').innerHTML=[
  metric('Страниц PDF',d.pages),metric('Просмотрено',sampled),
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
 summary.hidden=false;results.hidden=false;
}
form.addEventListener('submit',async e=>{
 e.preventDefault();if(button.disabled)return;
 const file=document.getElementById('pdf').files[0];
 if(!file){msg.textContent='Выбери PDF';return}
 if(file.size>25*1024*1024){msg.className='error';msg.textContent='PDF больше 25 МБ';return}
 button.disabled=true;summary.hidden=true;results.hidden=true;
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
  render(data);msg.className='good';msg.textContent='✓ PDF принят для анализа. Создание заданий отключено.';
 }catch(err){msg.className='error';msg.textContent=err.name==='AbortError'?
  'Обработка слишком долгая. Попробуй меньший файл для первого теста.':err.message;
 }finally{button.disabled=false}
});
</script></body></html>"""
