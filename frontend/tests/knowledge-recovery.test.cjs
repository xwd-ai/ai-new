const {test} = require('node:test');
const assert = require('node:assert/strict');
const {KnowledgeRecovery} = require('../knowledge-recovery.js');
const item={fingerprint:'fp/with space',knowledge_id:'k1',stage:'chunked',chunk_count:2,updated_at:123};
function setup(request) {
  const rows=[],statuses=[],results=[],busy=[],completed=[];
  const recovery=new KnowledgeRecovery({request,onRows:x=>rows.push(x),onStatus:x=>statuses.push(x),onResult:x=>results.push(x),
    onBusy:x=>busy.push(x),onRecovered:x=>completed.push(x)});
  return {recovery,rows,statuses,results,busy,completed};
}
test('load lists metadata; recovery posts encoded fingerprint once then refreshes',async()=>{
  const calls=[];let reads=0;
  const {recovery,completed,results}=setup(async(path,options)=>{calls.push([path,options]);
    return options?{knowledge_id:'k1',created:true,chunks:2}:{items:++reads===1?[item]:[]};});
  await recovery.load(); await recovery.recover(item.fingerprint);
  assert.equal(calls[1][0],'/api/knowledge/ingest/recover/fp%2Fwith%20space');
  assert.equal(calls[1][1].method,'POST'); assert.equal(calls.length,3);
  assert.equal(completed[0].knowledge_id,'k1');assert.deepEqual(recovery.items,[]);assert.match(results.at(-1),/恢复完成/);
});
test('double recovery and refresh while writing do not create parallel requests',async()=>{
  let resolve,calls=0;
  const {recovery}=setup(async(path,options)=>{calls++;return options?new Promise(r=>resolve=r):{items:[item]};});
  await recovery.load();const first=recovery.recover(item.fingerprint);
  await recovery.recover(item.fingerprint);await recovery.load();assert.equal(calls,2);
  resolve({knowledge_id:'k1',created:false,chunks:0});await first;assert.equal(calls,3);
});
test('old reads and responses after access revocation cannot restore rows',async()=>{
  const pending=[];const {recovery,rows,statuses,results}=setup(()=>new Promise(r=>pending.push(r)));
  const first=recovery.load(), second=recovery.load();pending[1]({items:[]});await second;
  pending[0]({items:[item]});await first;assert.deepEqual(rows.at(-1),[]);
  const third=recovery.load();recovery.clear();pending[2]({items:[item]});await third;assert.deepEqual(rows.at(-1),[]);
  assert.match(statuses.at(-1),/清空/);assert.equal(results.at(-1),'');
});
for(const error of ['记录不存在，请刷新','占用中，请稍后重试','网络连接失败','请求超时']) {
  test('recovery error is displayed without automatic retry: '+error,async()=>{
    let calls=0;const {recovery,results,completed}=setup(async(path,options)=>{calls++;if(options)throw Error(error);return {items:[item]};});
    await recovery.load();await recovery.recover(item.fingerprint);
    assert.equal(calls,2);assert.equal(recovery.recovering,false);assert.equal(completed.length,0);
    assert.match(results.at(-1),new RegExp(error));assert.match(results.at(-1),/刷新/);
  });
}
test('failed read clears stale entries; missing row cannot be recovered',async()=>{
  let fail=false,calls=0;const {recovery,rows,statuses}=setup(async()=>{calls++;if(fail)throw Error('读取失败');return {items:[item]};});
  await recovery.load();fail=true;await recovery.load();await recovery.recover(item.fingerprint);
  assert.equal(calls,2);assert.deepEqual(rows.at(-1),[]);assert.match(statuses.at(-1),/读取失败/);
});
