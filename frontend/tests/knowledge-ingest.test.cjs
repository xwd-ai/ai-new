const {test} = require('node:test');
const assert = require('node:assert/strict');
const {KnowledgeIngest} = require('../knowledge-ingest.js');
const input = () => ({title:'  导出说明  ', content:' 正文 ', kind:'manual', keywords:'导出, CSV，导出\n权限'});
function setup(request) {
  const statuses=[],busy=[],saved=[];
  const form=new KnowledgeIngest({request,onStatus:x=>statuses.push(x),onBusy:x=>busy.push(x),onSaved:x=>saved.push(x)});
  return {form,statuses,busy,saved};
}
test('normalizes text and keywords and sends one JSON request', async()=>{
  let call; const result={knowledge_id:'k1',created:true,chunks:2};
  const {form,saved,statuses,busy}=setup(async(...args)=>{call=args;return result;});
  await form.submit(input());
  assert.equal(call[0],'/api/knowledge/ingest'); assert.equal(call[1].method,'POST');
  assert.deepEqual(JSON.parse(call[1].body),{title:'导出说明',content:'正文',kind:'manual',keywords:['导出','CSV','权限']});
  assert.deepEqual(saved,[result]); assert.deepEqual(busy,[true,false]); assert.match(statuses.at(-1),/新增/);
});
for(const patch of [{title:' '},{content:''},{kind:'other'},{title:'x'.repeat(201)},{content:'x'.repeat(100001)},{keywords:Array.from({length:21},(_,i)=>i).join(',')}]) {
  test('rejects invalid input without HTTP '+Object.keys(patch)[0]+JSON.stringify(patch).length,async()=>{
    let calls=0;const {form,statuses}=setup(async()=>{calls++;});
    await form.submit({...input(),...patch});assert.equal(calls,0);assert.ok(statuses.at(-1));
  });
}
test('double submit cannot create parallel writes; empty keywords allow extraction',async()=>{
  let resolve,calls=0; const {form,busy}=setup(async(path,options)=>{calls++;assert.deepEqual(JSON.parse(options.body).keywords,[]); return new Promise(r=>resolve=r);});
  const first=form.submit({...input(),keywords:''}); await form.submit(input()); assert.equal(calls,1);
  resolve({knowledge_id:'k',created:false,chunks:0}); await first; assert.deepEqual(busy,[true,false]);
});
test('duplicate result reports existing source; failure unlocks and never retries automatically',async()=>{
  let calls=0;const {form,statuses,saved}=setup(async()=>{if(++calls===1)throw Error('请求超时');return {knowledge_id:'old',created:false,chunks:0};});
  const original=input(); await form.submit(original);assert.equal(calls,1);assert.equal(form.busy,false);assert.deepEqual(original,input());
  assert.match(statuses.at(-1),/保留/);assert.equal(saved.length,0);
  await form.submit(original);assert.match(statuses.at(-1),/已存在/);assert.equal(saved[0].knowledge_id,'old');
});

test('uploads PDF or Markdown as a binary body with file metadata', async () => {
  for (const name of ['扫描手册.pdf', 'notes.md']) {
    const file = {name, size: 100};
    let call;
    const {form, saved} = setup(async (...args) => {
      call = args;
      return {created: true, chunks: 1, knowledge_id: 'new'};
    });
    await form.submit({...input(), title: '', content: '', file});
    const url = new URL(call[0], 'http://localhost');
    assert.equal(url.pathname, '/api/knowledge/ingest/file');
    assert.equal(url.searchParams.get('filename'), name);
    assert.deepEqual(url.searchParams.getAll('keywords'), ['导出', 'CSV', '权限']);
    assert.equal(call[1].body, file);
    assert.equal(call[1].headers['Content-Type'], 'application/octet-stream');
    assert.equal(saved[0].knowledge_id, 'new');
  }
});
