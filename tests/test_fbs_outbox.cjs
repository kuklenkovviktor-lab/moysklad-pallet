const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync('wms.py','utf8');
const start=source.indexOf("const FBS_OUTBOX_KEY=");
const end=source.indexOf('async function handleScan(code){',start);
assert(start>=0&&end>start);
const shared=new Map();let counted=0,seen=new Set(),lost=true,bodies=[];
function browser(storage=shared,fetcher){
 const ctx={console,crypto:{randomUUID:()=> '00000000-0000-4000-8000-000000000001'},
 localStorage:{getItem:k=>storage.get(k)||null,setItem:(k,v)=>storage.set(k,v),removeItem:k=>storage.delete(k)},
 document:{getElementById:()=>({textContent:''})},updateScanStage(){},beep(){},showTsdError(){},refresh:async()=>{},
 fetch:fetcher||(async(url,options)=>{
  const p=JSON.parse(options.body);bodies.push(p);
  const replayed=seen.has(p.request_id);
  if(!replayed){seen.add(p.request_id);counted++}
  if(lost){lost=false;throw Error('lost acknowledgement')}
  return {ok:true,status:200,json:async()=>({ok:true,replayed})};
 })};
 vm.createContext(ctx);vm.runInContext("var shiftId='shift';var pendingProduct={barcode:'product',article:'SKU',name:'Test'};"+source.slice(start,end),ctx);return ctx;
}
(async()=>{
 let ctx=browser();assert.equal(await ctx.acceptWb('WB'),false);assert.equal(counted,1);assert.equal(shared.size,1);
 const original=JSON.parse([...shared.values()][0]);
 ctx=browser();assert.equal(await ctx.retryFbsOutbox(),true);assert.equal(counted,1);assert.equal(shared.size,0);assert.equal(bodies[1].request_id,original.request_id);
 const failing=browser(new Map(),async()=>({ok:false,status:503,json:async()=>({})}));await failing.acceptWb('WB');assert(failing.queuedFbsScan());
 const invalid=browser(new Map(),async()=>({ok:false,status:409,json:async()=>({error:'wrong item'})}));await invalid.acceptWb('WB');assert.equal(invalid.queuedFbsScan(),null);
 let requests=0;ctx=browser(new Map(),async()=>{requests++;throw Error('must not send')});ctx.localStorage.setItem=()=>{throw Error('storage unavailable')};await assert.rejects(()=>ctx.acceptWb('WB'));assert.equal(requests,0);
 console.log('FBS outbox: PASS (lost ACK, reload/replay, server failure retention, definitive rejection, storage failure before send)');
})().catch(e=>{console.error(e);process.exit(1)});
