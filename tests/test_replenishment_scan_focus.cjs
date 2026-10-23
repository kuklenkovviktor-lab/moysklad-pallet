// Simulate HID scanner focus without a browser or network.
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const source=fs.readFileSync('replenishment.py','utf8');
const begin=source.indexOf('function focusTaskInput(');
const end=source.indexOf('async function load(',begin);
assert(begin>=0&&end>begin,'scanner focus helpers not found');
const focusHelpers=source.slice(begin,end);
let focused=null;
let active={id:'body'};
const fields={};
for(const id of ['scan-task1','qty-task1','scan-task2','qty-task2']){
  fields[id]={id,value:'old-scan',isConnected:true,focus(){focused=id}};
}
const ctx={tasks:[],cameraActive:false,document:{
  get activeElement(){return active},
  getElementById(id){return fields[id]||null}
},requestAnimationFrame(fn){fn()}};
vm.createContext(ctx);
vm.runInContext(focusHelpers,ctx);
const task=(id,product,location)=>({
  id,status:'started',verify_required:true,
  verified_product_at:product?'2026-10-23':null,
  verified_location_at:location?'2026-10-23':null
});
ctx.tasks=[task('task1',false,false)];
ctx.focusTaskInput('task1',true);
assert.equal(focused,'scan-task1','new task must focus product scan input');
ctx.tasks=[task('task1',true,false)];
focused=null;
ctx.focusTaskInput('task1',true);
assert.equal(focused,'scan-task1','after product verification focus the newly rendered location scan');
ctx.tasks=[task('task1',true,true)];
focused=null;
ctx.focusTaskInput('task1',true);
assert.equal(focused,'qty-task1','after location verification focus quantity');
ctx.tasks=[task('task1',false,false),task('task2',true,false)];
focused=null;
ctx.focusTaskInput('task2',true);
assert.equal(focused,'scan-task2','focus the task actually accepted');
active={id:'selfBarcode'};
focused=null;
ctx.focusTaskInput('task1',false);
assert.equal(focused,null,'automatic refresh must not steal self-create input focus');
ctx.focusTaskInput('task1',true);
assert.equal(focused,'scan-task1','explicit transition should focus the accepted task');
active={id:'body'};
ctx.cameraActive=true;
focused=null;
ctx.focusTaskInput('task1',true);
assert.equal(focused,null,'do not steal focus while mobile camera is open');
ctx.cameraActive=false;
fields['scan-task1'].value='incorrect';
ctx.retryScanFocus('task1');
assert.equal(fields['scan-task1'].value,'','bad scan must clear input');
assert.equal(focused,'scan-task1','bad scan must restore focus for retry');
assert(source.includes("await load(type==='start'?id:null,type==='start')"),'start must request focus restoration');
assert(source.includes('await load(id,true);'), 'verification must request focus restoration');
assert(source.includes('await load(d.task_id,true);'),'self-created task must focus scan input');
console.log('Replenishment HID scanner focus transitions: PASS (8 checks)');
