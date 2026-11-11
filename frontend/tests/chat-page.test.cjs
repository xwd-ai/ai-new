const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.join(__dirname, '..');
const html = fs.readFileSync(path.join(root, 'index.html'), 'utf8');
const settle = async () => { for (let i=0;i<8;i++) await new Promise(setImmediate); };
const json = (data, status=200) => ({ok:status>=200&&status<300,status,json:async()=>data});

// 只实现实际脚本使用的 DOM 接口，不验证布局、HTML解析或真实时间。
class Element {
  constructor(tag='div') { this.tag=tag; this.children=[]; this.listeners={}; this.style={}; this.value=''; this.hidden=false; this.disabled=false; this.className=''; this.text=''; }
  appendChild(child) { this.children.push(child); return child; }
  replaceChildren(...children) { this.children=children; this.text=''; }
  set textContent(value) { this.text=value; this.children=[]; }
  get textContent() { return this.text+this.children.map(x=>x.textContent).join(''); }
  set innerHTML(value) {
    this.replaceChildren();
    for (const match of value.matchAll(/<(?:div|button) class="([^"]+)"/g)) {
      const child=new Element(); child.className=match[1]; this.appendChild(child);
    }
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  querySelectorAll(selector) {
    return this.children.flatMap(child=>[
      ...(child.className.split(' ').includes(selector.slice(1))?[child]:[]), ...child.querySelectorAll(selector),
    ]);
  }
  addEventListener(name, fn) { (this.listeners[name] ||= []).push(fn); }
  async emit(name, extra={}) {
    for (const fn of this.listeners[name] || []) fn({preventDefault(){},...extra});
    await settle();
  }
  focus() { this.focused=true; }
}

function page(handler=()=>null, rows=[{role:'user',content:'saved'}]) {
  const elements={};
  for (const match of html.matchAll(/<([\w-]+)\b[^>]*\bid="([^"]+)"[^>]*>/g)) {
    const element=elements[match[2]]=new Element(match[1]); element.hidden=/\bhidden\b/.test(match[0]);
  }
  elements.chatLog.textContent='welcome';
  const calls=[], timers=[], browser=new Element(), storage=new Map([
    ['token','test-token'],['user',JSON.stringify({id:'u',name:'客户',role:'customer'})],
  ]);
  browser.location={href:'/'};
  let reloads=0;
  browser.location.reload=()=>{reloads++;};
  const navigator={onLine:true};
  const context=vm.createContext({console, navigator, AbortController, window:browser,
    document:{getElementById:id=>{assert.ok(elements[id],`missing ${id}`); return elements[id];},
      createElement:tag=>new Element(tag), querySelector:()=>new Element(), querySelectorAll:()=>[]},
    localStorage:{getItem:key=>storage.get(key),removeItem:key=>storage.delete(key)},
    AbortSignal:{timeout:budget=>({budget,throwIfAborted(){}}),any:signals=>{signals[0].budget=signals[1].budget;return signals[0];}},
    setTimeout:fn=>{timers.push(fn);return timers.length;},
    fetch:async(url,options)=>{
      calls.push({url,...options}); const response=await handler(url,options); if(response) return response;
      if(url==='/api/me')return json({user:{id:'u'}});
      if(url.startsWith('/api/conversation?')) {
        const offset=Number(new URLSearchParams(url.split('?')[1]).get('offset'));
        return json({messages:rows.slice(offset,offset+100),total:rows.length});
      }
      if(url==='/api/chat')return json({reply:'answer',sources:[],trace:[],suggest_ticket:false});
      if(url==='/api/order')return json({order:null});
      throw Error('unexpected URL '+url);
    },
  });
  const scripts=[...html.matchAll(/<script(?: src="([^"]+)")?>([\s\S]*?)<\/script>/g)];
  assert.deepEqual(scripts.filter(x=>x[1]).map(x=>x[1]),['/assets/chat-recovery.js']);
  for (const [,src,inline] of scripts) vm.runInContext(src?fs.readFileSync(path.join(root,src.replace('/assets/','')),'utf8'):inline,context,{filename:src||'index-inline.js'});
  return {elements,calls,browser,navigator,timers,storage,get reloads(){return reloads;},
    async retry(){assert.ok(timers.length);timers.shift()();await settle();}};
}
const reads = calls => calls.filter(x=>x.url.startsWith('/api/conversation?'));
const contents = log => log.children.map(x=>x.querySelector('.bubble').textContent);
const rows = Array.from({length:205},(_,i)=>({role:i%2?'assistant':'user',content:`saved-${i}`}));

test('pagehide cancels paging and cached return synchronizes without losing the draft',async()=>{
  let release,hold=true;
  const {elements:e,calls,browser}=page(url=>hold&&url.includes('offset=100')?new Promise(r=>release=r):null,rows);
  await settle();e.msgInput.value='draft';const oldSignal=reads(calls)[0].signal;
  await browser.emit('pagehide',{persisted:true});assert.equal(oldSignal.aborted,true);
  const count=calls.length;await browser.emit('online');assert.equal(calls.length,count);
  hold=false;await browser.emit('pageshow',{persisted:true});
  assert.equal(e.chatLog.children.length,205);assert.equal(e.msgInput.value,'draft');
  release(json({messages:rows.slice(100,200),total:205}));await settle();
  assert.equal(e.chatLog.children.length,205);assert.equal(reads(calls).length,5);
});

test('ordinary pageshow does not duplicate initial synchronization',async()=>{
  const {calls,browser}=page();await settle();await browser.emit('pageshow',{persisted:false});
  assert.equal(reads(calls).length,1);
});

test('queued history waits for cached return when a send finishes off-page',async()=>{
  let reply;const {elements:e,calls,browser}=page(url=>url==='/api/chat'?new Promise(r=>reply=r):null);
  await settle();e.msgInput.value='question';await e.sendBtn.emit('click');await browser.emit('online');
  await browser.emit('pagehide',{persisted:true});reply(json({reply:'answer'}));await settle();
  assert.equal(reads(calls).length,1);
  await browser.emit('pageshow',{persisted:true});assert.equal(reads(calls).length,2);
  assert.equal(calls.filter(x=>x.method==='POST').length,1);
});

test('cached return reloads after credentials change before reading history',async()=>{
  const state=page(url=>url==='/api/chat'?json({reply:'answer',suggest_ticket:true}):null);
  await settle();state.elements.msgInput.value='initial';await state.elements.sendBtn.emit('click');
  const confirm=state.elements.chatLog.querySelector('.ct-btn');assert.ok(confirm);
  await state.browser.emit('pagehide',{persisted:true});
  state.storage.set('token','changed');await state.browser.emit('pageshow',{persisted:true});
  assert.equal(state.reloads,1);assert.equal(reads(state.calls).length,1);
  state.elements.msgInput.value='draft';await state.elements.sendBtn.emit('click');await confirm.emit('click');
  assert.equal(state.calls.filter(x=>x.method==='POST').length,1);
  assert.equal(state.elements.msgInput.value,'draft');
});

test('actual scripts initialize all pages, manual sync replaces and preserves draft',async()=>{
  const {elements:e,calls}=page(()=>null,rows); await settle();
  assert.deepEqual(contents(e.chatLog),rows.map(x=>x.content)); assert.equal(reads(calls).length,3);
  assert.ok(calls.filter(x=>x.url==='/api/me'||x.url.startsWith('/api/conversation?')).every(x=>x.headers.Authorization==='Bearer test-token'));
  assert.ok(reads(calls).every(x=>x.signal===reads(calls)[0].signal && x.signal.budget===10000));
  e.msgInput.value='draft'; await e.syncHistory.emit('click');
  assert.equal(e.msgInput.value,'draft'); assert.equal(e.chatLog.children.length,205);
  assert.equal(reads(calls).length,6); assert.equal(e.sendBtn.disabled,false);
});

test('later page blocks send and preserves old display until all pages complete',async()=>{
  let release; const {elements:e,calls}=page(url=>url.includes('offset=100')?new Promise(r=>release=r):null,rows);
  await settle(); assert.equal(e.chatLog.textContent,'welcome'); assert.equal(e.sendBtn.disabled,true);
  assert.match(e.connectionStatus.textContent,/已读取 100\/205 条/);
  e.msgInput.value='draft'; await e.sendBtn.emit('click');
  assert.equal(calls.some(x=>x.method==='POST'),false); assert.equal(e.msgInput.value,'draft');
  release(json({messages:rows.slice(100,200),total:205})); await settle();
  assert.equal(e.chatLog.children.length,205); assert.equal(e.sendBtn.disabled,false);
});

test('cancel button aborts paging, preserves draft and ignores late page without more reads',async()=>{
  let release,hold=true;
  const {elements:e,calls}=page(url=>hold&&url.includes('offset=100')?new Promise(r=>release=r):null,rows);
  await settle();assert.equal(e.cancelHistory.hidden,false);e.msgInput.value='draft';
  const signal=reads(calls)[0].signal;
  await e.cancelHistory.emit('click');assert.equal(signal.aborted,true);
  assert.equal(e.cancelHistory.hidden,true);assert.equal(e.sendBtn.disabled,false);
  assert.equal(e.msgInput.value,'draft');assert.equal(e.chatLog.textContent,'welcome');
  const count=calls.length,status=e.connectionStatus.textContent;
  release(json({messages:rows.slice(100,200),total:205}));await settle();
  assert.equal(calls.length,count);assert.equal(e.connectionStatus.textContent,status);
  hold=false;await e.syncHistory.emit('click');assert.equal(e.chatLog.children.length,205);
});

test('cancel during retry wait does not resume after the old timer fires',async()=>{
  const state=page(url=>url.startsWith('/api/conversation?')?json({},503):null);
  await settle();assert.equal(state.elements.cancelHistory.hidden,false);
  await state.elements.cancelHistory.emit('click');const count=state.calls.length;
  await state.retry();assert.equal(state.calls.length,count);assert.match(state.elements.connectionStatus.textContent,/已取消/);
});

test('cancel during final identity check prevents complete history from being displayed',async()=>{
  let ids=0,release;const {elements:e}=page(url=>url==='/api/me'&&++ids===2?new Promise(r=>release=r):null);
  await settle();await e.cancelHistory.emit('click');
  release(json({user:{id:'u'}}));await settle();
  assert.equal(e.chatLog.textContent,'welcome');assert.match(e.connectionStatus.textContent,/已取消/);
});

test('online event synchronizes while offline event and offline reads preserve draft',async()=>{
  const {elements:e,calls,browser,navigator}=page(); await settle();
  const count=reads(calls).length; e.msgInput.value='draft'; navigator.onLine=false;
  await browser.emit('offline'); await e.syncHistory.emit('click');
  assert.equal(reads(calls).length,count); assert.match(e.connectionStatus.textContent,/断开/);
  navigator.onLine=true; await browser.emit('online');
  assert.equal(reads(calls).length,count+1); assert.equal(e.msgInput.value,'draft');
});

test('retry after a partial read restarts at zero and never paints the partial page',async()=>{
  let failed=false,release;
  const state=page(url=>{if(url.includes('offset=100')){
    if(!failed){failed=true;throw Error('network');}
    return new Promise(r=>release=r);
  }},rows);
  await settle(); assert.equal(state.elements.chatLog.textContent,'welcome');
  await state.retry();
  assert.match(state.elements.connectionStatus.textContent,/重试同步历史（2\/3）.*已读取 100\/205 条/);
  release(json({messages:rows.slice(100,200),total:205})); await settle();
  assert.deepEqual(reads(state.calls).map(x=>new URLSearchParams(x.url.split('?')[1]).get('offset')),['0','100','0','100','200']);
  assert.equal(state.elements.chatLog.children.length,205);
});

test('last page progress waits for final identity before reporting success',async()=>{
  let identities=0,release;
  const {elements:e}=page(url=>url==='/api/me'&&++identities===2?new Promise(r=>release=r):null);
  await settle(); assert.match(e.connectionStatus.textContent,/已读取 1\/1 条.*正在核对身份/);
  assert.equal(e.chatLog.textContent,'welcome');
  release(json({user:null}));await settle();
  assert.match(e.connectionStatus.textContent,/重新登录/);assert.equal(e.chatLog.textContent,'welcome');
});

test('uncertain POST is never retried and its text survives full history sync',async()=>{
  const {elements:e,calls}=page(url=>url==='/api/chat'?json({},503):null); await settle();
  e.msgInput.value='unconfirmed'; await e.sendBtn.emit('click');
  assert.equal(e.uncertainQuestions.hidden,false); assert.match(e.uncertainTexts.textContent,/unconfirmed/);
  e.msgInput.value='new draft'; await e.syncHistory.emit('click');
  assert.match(e.uncertainTexts.textContent,/unconfirmed/); assert.equal(e.msgInput.value,'new draft');
  assert.equal(calls.filter(x=>x.method==='POST').length,1);
});

test('online sync during a write is deferred and send cannot duplicate',async()=>{
  let release; const {elements:e,calls,browser}=page(url=>url==='/api/chat'?new Promise(r=>release=r):null);
  await settle(); e.msgInput.value='question'; await e.sendBtn.emit('click');
  const count=reads(calls).length; await browser.emit('online'); e.msgInput.value='next draft'; await e.sendBtn.emit('click');
  assert.equal(reads(calls).length,count); assert.equal(calls.filter(x=>x.method==='POST').length,1);
  release(json({reply:'answer',sources:[],trace:[]})); await settle();
  assert.equal(reads(calls).length,count+1); assert.equal(e.msgInput.value,'next draft');
});

test('final identity mismatch cannot repaint history and is not retried',async()=>{
  let identities=0; const {elements:e,calls,timers}=page(url=>url==='/api/me'?json({user:{id:++identities===1?'u':'other'}}):null);
  await settle(); assert.equal(e.chatLog.textContent,'welcome'); assert.match(e.connectionStatus.textContent,/重新登录/);
  assert.equal(timers.length,0); assert.equal(reads(calls).length,1); assert.equal(e.sendBtn.disabled,false);
});

for (const status of [401,403]) {
  test('HTTP '+status+' during paging stops retries and keeps display',async()=>{
    const {elements:e,calls,timers}=page(url=>url.startsWith('/api/conversation?')?json({},status):null);
    await settle(); assert.equal(e.chatLog.textContent,'welcome'); assert.match(e.connectionStatus.textContent,/重新登录/);
    assert.equal(reads(calls).length,1); assert.equal(timers.length,0); assert.equal(e.syncHistory.disabled,false);
  });
}

test('exhausted page failures preserve old messages and permit manual sync',async()=>{
  let fail=false; const state=page(url=>fail&&url.startsWith('/api/conversation?')?json({},503):null);
  await settle(); const before=state.elements.chatLog.textContent; fail=true;
  await state.elements.syncHistory.emit('click'); await state.retry(); await state.retry();
  assert.match(state.elements.connectionStatus.textContent,/同步失败/);
  assert.equal(state.elements.chatLog.textContent,before); assert.equal(state.elements.syncHistory.disabled,false);
  fail=false; await state.elements.syncHistory.emit('click');
  assert.match(state.elements.connectionStatus.textContent,/历史已同步/);
});
