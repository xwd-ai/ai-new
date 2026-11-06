const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.join(__dirname, '..');
const html = fs.readFileSync(path.join(root, 'knowledge.html'), 'utf8');
const settle = async () => { for(let i=0;i<5;i++) await new Promise(setImmediate); };
const json = (data, status=200) => new Response(JSON.stringify(data), {status,headers:{'Content-Type':'application/json'}});
const doc = {id:'k1',title:'<b>资料</b>',kind:'faq'};
const row = {fingerprint:'fp',knowledge_id:'k1',stage:'chunked',chunk_count:1,updated_at:123};
const isPending = url => url.startsWith('/api/knowledge/ingest/pending?');

// 仅模拟脚本实际使用的 DOM 接口；不模拟布局、HTML 校验或真实网络。
class Element {
  constructor(tag='div') { this.tag=tag;this.children=[];this.listeners={};this.disabled=false;this.hidden=false;this.value='';this.textContent=''; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children=children; }
  addEventListener(name, callback) { (this.listeners[name] ||= []).push(callback); }
  querySelectorAll(tag) { return this.children.flatMap(x=>[...(x.tag===tag?[x]:[]),...x.querySelectorAll(tag)]); }
  async emit(name) { for(const callback of this.listeners[name] || []) await callback({preventDefault(){}}); await settle(); }
}
function page(handler=()=>null) {
  const elements={};
  for(const match of html.matchAll(/<([\w-]+)\b[^>]*\bid="([^"]+)"[^>]*>/g)) {
    const node=elements[match[2]]=new Element(match[1]);
    node.disabled=/\bdisabled\b/.test(match[0]);node.hidden=/\bhidden\b/.test(match[0]);
  }
  elements.ingestKind.value='manual';
  const calls=[];
  const context=vm.createContext({console,URLSearchParams,TypeError,
    document:{getElementById:id=>{assert.ok(elements[id],`missing element ${id}`);return elements[id];},createElement:tag=>new Element(tag)},
    localStorage:{getItem:key=>key==='token'?'test-token':null},
    AbortSignal:{timeout:ms=>({budget:ms})},
    fetch:async(url,options)=>{
      calls.push({url,...options});const custom=await handler(url,options);
      if(custom) return custom;
      if(url==='/api/me')return json({user:{role:'admin',name:'管理员'}});
      if(url.startsWith('/api/knowledge/items?'))return json({items:[doc],total:1});
      if(isPending(url))return json({items:[row],total:1});
      if(url.startsWith('/api/sources/'))return json({...doc,content:'<script>原文</script>',keywords:['资料']});
      return json({knowledge_id:'k1',created:true,chunks:1});
    },
  });
  const scripts=[...html.matchAll(/<script src="\/assets\/([^"]+)" defer><\/script>/g)].map(x=>x[1]);
  assert.deepEqual(scripts,['knowledge-ingest.js','knowledge-recovery.js','knowledge-browser.js']);
  for(const script of scripts)vm.runInContext(fs.readFileSync(path.join(root,script),'utf8'),context,{filename:script});
  return {elements,calls};
}
function fill(elements) { elements.ingestTitle.value='标题';elements.ingestContent.value='正文';elements.ingestKeywords.value='关键,词'; }

test('actual scripts initialize, wire filters and render source text literally',async()=>{
  const {elements:e,calls}=page();await settle();
  assert.equal(e.filters.disabled,false);assert.equal(e.ingestFields.disabled,false);
  assert.equal(calls[0].headers.Authorization,'Bearer test-token');assert.equal(calls[0].signal.budget,20000);
  e.title.value='A&B';e.kind.value='faq';await e.searchForm.emit('submit');
  const query=new URLSearchParams(calls.at(-1).url.split('?')[1]);assert.equal(query.get('title'),'A&B');assert.equal(query.get('kind'),'faq');
  await e.rows.querySelectorAll('button')[0].emit('click');
  assert.equal(e.content.textContent,'<script>原文</script>');assert.equal(e.detail.hidden,false);
  await e.closeDetail.emit('click');assert.equal(e.detail.hidden,true);
});
for(const identity of [json({user:{role:'customer'}}),json({},401)]) {
  test('non-admin initialization never loads data or enables write controls '+identity.status,async()=>{
    const {elements:e,calls}=page(url=>url==='/api/me'?identity:null);await settle();
    assert.equal(e.filters.disabled,true);assert.equal(e.ingestFields.disabled,true);assert.equal(e.loginLink.hidden,false);
    await e.ingestForm.emit('submit');await e.refreshPending.emit('click');assert.equal(calls.length,1);
  });
}
test('form JSON, duplicate result, raw source link and refresh are wired',async()=>{
  const {elements:e,calls}=page(url=>url==='/api/knowledge/ingest'?json({knowledge_id:'k1',created:false,chunks:0}):null);await settle();fill(e);
  await e.ingestForm.emit('submit');const post=calls.find(x=>x.method==='POST');
  assert.equal(post.url,'/api/knowledge/ingest');assert.equal(post.signal.budget,60000);
  assert.equal(JSON.parse(post.body).content,'正文');assert.match(e.ingestStatus.textContent,/已存在/);
  assert.equal(e.viewSaved.hidden,false);await e.viewSaved.emit('click');assert.equal(e.detail.hidden,false);
  await e.ingestForm.emit('input');assert.equal(e.viewSaved.hidden,true);
});
test('timeout preserves input, releases form and never retries POST',async()=>{
  const {elements:e,calls}=page(url=>{if(url==='/api/knowledge/ingest'){const error=Error();error.name='TimeoutError';throw error;}});await settle();fill(e);
  await e.ingestForm.emit('submit');assert.equal(e.ingestContent.value,'正文');assert.equal(e.ingestFields.disabled,false);
  assert.match(e.ingestStatus.textContent,/保存结果尚未确认/);assert.equal(calls.filter(x=>x.method==='POST').length,1);
});
test('pending write disables form and a second submit cannot send another POST',async()=>{
  let resolve;const {elements:e,calls}=page(url=>url==='/api/knowledge/ingest'?new Promise(r=>resolve=r):null);
  await settle();fill(e);await e.ingestForm.emit('submit');assert.equal(e.ingestFields.disabled,true);
  await e.ingestForm.emit('submit');assert.equal(calls.filter(x=>x.method==='POST').length,1);
  resolve(json({knowledge_id:'k1',created:true,chunks:1}));await settle();assert.equal(e.ingestFields.disabled,false);
});
test('initial network failure stays read-only and shows recovery direction',async()=>{
  const {elements:e,calls}=page(()=>{throw new TypeError('offline');});await settle();
  assert.equal(e.filters.disabled,true);assert.equal(e.ingestFields.disabled,true);assert.equal(e.refreshPending.disabled,true);
  assert.match(e.access.textContent,/连接/);assert.equal(e.loginLink.hidden,false);assert.equal(calls.length,1);
});
for(const code of [404,409,422,500]) {
  test('recovery HTTP '+code+' is surfaced and controls unlock',async()=>{
    const {elements:e,calls}=page(url=>url.startsWith('/api/knowledge/ingest/recover/')?json({},code):null);await settle();
    await e.refreshPending.emit('click');await e.pendingRows.querySelectorAll('button')[0].emit('click');
    assert.equal(e.refreshPending.disabled,false);assert.match(e.recoveryStatus.textContent,/刷新/);
    assert.equal(calls.filter(x=>x.method==='POST').length,1);
    if(code===422)assert.match(e.recoveryStatus.textContent,/人工核对/);
  });
}
test('recovery success refreshes pending and knowledge lists',async()=>{
  const {elements:e,calls}=page();await settle();await e.refreshPending.emit('click');
  await e.pendingRows.querySelectorAll('button')[0].emit('click');assert.match(e.recoveryStatus.textContent,/恢复完成/);
  assert.equal(calls.filter(x=>isPending(x.url)).length,2);
  assert.equal(calls.filter(x=>x.url.startsWith('/api/knowledge/items?')).length,2);
});
test('late write success after 403 does not restore saved actions or issue follow-up reads',async()=>{
  let resolve;
  const {elements:e,calls}=page(url=>url==='/api/knowledge/ingest'?new Promise(r=>resolve=r):
    isPending(url)?json({},403):null);await settle();fill(e);
  const writing=e.ingestForm.emit('submit');await settle();await e.refreshPending.emit('click');const count=calls.length;
  resolve(json({knowledge_id:'k1',created:true,chunks:1}));await writing;await settle();
  assert.equal(e.ingestFields.disabled,true);assert.equal(e.viewSaved.hidden,true);assert.equal(calls.length,count);
});
test('late recovery success after 403 cannot fetch pending records again',async()=>{
  let resolve, denied=false;
  const {elements:e,calls}=page(url=>url.startsWith('/api/knowledge/ingest/recover/')?new Promise(r=>resolve=r):
    url.startsWith('/api/knowledge/items?') && denied?json({},403):null);
  await settle();await e.refreshPending.emit('click');await e.pendingRows.querySelectorAll('button')[0].emit('click');
  denied=true;await e.searchForm.emit('submit');const count=calls.length;
  resolve(json({knowledge_id:'k1',created:true,chunks:1}));await settle();
  assert.equal(calls.length,count);assert.equal(e.refreshPending.disabled,true);assert.equal(e.pendingRows.children.length,0);
  assert.equal(e.pendingPrevious.disabled, true);
  assert.equal(e.pendingNext.disabled, true);
  assert.doesNotMatch(e.recoveryStatus.textContent,/恢复完成/);
});
test('late list success after 403 cannot replace login-required status',async()=>{
  let resolve;
  const {elements:e}=page(url=>url.startsWith('/api/knowledge/items?')?new Promise(r=>resolve=r):
    isPending(url)?json({},403):null);await settle();await e.refreshPending.emit('click');
  resolve(json({items:[doc],total:1}));await settle();assert.equal(e.rows.children.length,0);assert.match(e.status.textContent,/登录/);
});

test('pending controls navigate and refresh returns to page one', async () => {
  const {elements: e, calls} = page(url => {
    if (isPending(url)) return json({items: [row], total: 21});
  });
  await settle();
  assert.equal(e.pendingPrevious.disabled, true);
  assert.equal(e.pendingNext.disabled, true);
  await e.refreshPending.emit('click');
  assert.equal(e.pendingNext.disabled, false);
  await e.pendingNext.emit('click');
  assert.match(e.pendingStatus.textContent, /共 21 条.*21–21/);
  assert.equal(e.pendingPrevious.disabled, false);
  assert.equal(e.pendingNext.disabled, true);
  await e.pendingPrevious.emit('click');
  await e.pendingNext.emit('click');
  await e.refreshPending.emit('click');
  assert.equal(e.pendingPrevious.disabled, true);
  assert.deepEqual(calls.filter(x => isPending(x.url)).map(x => new URLSearchParams(x.url.split('?')[1]).get('offset')),
    ['0', '20', '0', '20', '0']);
});
