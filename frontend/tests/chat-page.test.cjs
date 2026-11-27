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
      ...(selector.slice(1).split('.').every(name=>child.className.split(' ').includes(name))?[child]:[]), ...child.querySelectorAll(selector),
    ]);
  }
  addEventListener(name, fn) { (this.listeners[name] ||= []).push(fn); }
  async emit(name, extra={}) {
    for (const fn of this.listeners[name] || []) fn({preventDefault(){},...extra});
    await settle();
  }
  setAttribute(name,value) { this[name]=value; }
  focus() { this.focused=true; }
  select() { this.selected=true; }
}

function page(handler=()=>null, rows=[{role:'user',content:'saved'}]) {
  const elements={};
  for (const match of html.matchAll(/<([\w-]+)\b[^>]*\bid="([^"]+)"[^>]*>/g)) {
    const element=elements[match[2]]=new Element(match[1]); element.hidden=/\bhidden\b/.test(match[0]);
  }
  elements.chatLog.textContent='welcome';
  const chatScroll=Object.assign(new Element(),{
    clientHeight:100,
  });
  Object.defineProperties(chatScroll,{
    scrollHeight:{get(){return Math.max(this.clientHeight,elements.chatLog.children.length*20);}},
    scrollTop:{
      get(){return this.position||0;},
      set(value){this.position=Math.max(0,Math.min(value,this.scrollHeight-this.clientHeight));},
    },
  });
  const calls=[], timers=[], timeouts=[], browser=new Element(), storage=new Map([
    ['token','test-token'],['user',JSON.stringify({id:'u',name:'客户',role:'customer'})],
  ]);
  browser.location={href:'/'};
  let reloads=0;
  browser.location.reload=()=>{reloads++;};
  const navigator={onLine:true};
  const context=vm.createContext({console, navigator, AbortController, window:browser,
    document:{getElementById:id=>{assert.ok(elements[id],`missing ${id}`); return elements[id];},
      createElement:tag=>new Element(tag), querySelector:selector=>selector==='.chat-scroll'?chatScroll:new Element(), querySelectorAll:()=>[]},
    localStorage:{getItem:key=>storage.get(key),removeItem:key=>storage.delete(key)},
    AbortSignal:{timeout:budget=>{
      const signal={budget,throwIfAborted(){}};
      timeouts.push(signal);
      return signal;
    },any:signals=>{signals[0].budget=signals[1].budget;return signals[0];}},
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
      if(url==='/api/conversations')return json({conversations:[]});
      if(url==='/api/tickets' && !options.method)return json({tickets:[],total:0,page:1,pages:0});
      throw Error('unexpected URL '+url);
    },
  });
  const scripts=[...html.matchAll(/<script(?: src="([^"]+)")?>([\s\S]*?)<\/script>/g)];
  assert.deepEqual(scripts.filter(x=>x[1]).map(x=>x[1]),['/assets/chat-recovery.js']);
  for (const [,src,inline] of scripts) vm.runInContext(src?fs.readFileSync(path.join(root,src.replace('/assets/','')),'utf8'):inline,context,{filename:src||'index-inline.js'});
  return {elements,calls,browser,navigator,timers,timeouts,storage,chatScroll,fillAndSend:context.fillAndSend,get reloads(){return reloads;},
    async retry(){assert.ok(timers.length);timers.shift()();await settle();}};
}
const reads = calls => calls.filter(x=>x.url.startsWith('/api/conversation?'));
const contents = log => log.children.map(x=>x.querySelector('.bubble').textContent);
const rows = Array.from({length:205},(_,i)=>({role:i%2?'assistant':'user',content:`saved-${i}`}));

test('history replay preserves reading position and follows new messages at the bottom',async()=>{
  const history=rows.slice(0,10);
  const {elements:e,chatScroll}=page(()=>null,history);
  await settle();
  const original=e.chatLog.children[0];
  assert.equal(chatScroll.scrollTop,100);
  chatScroll.scrollTop=40;
  history.push(rows[10]);
  await e.syncHistory.emit('click');
  assert.equal(chatScroll.scrollTop,40);
  assert.equal(e.chatLog.children[0],original);
  assert.equal(e.chatLog.children.length,11);
  chatScroll.scrollTop=119;
  history.push(rows[11]);
  await e.syncHistory.emit('click');
  assert.equal(chatScroll.scrollTop,140);
  assert.equal(e.chatLog.children[0],original);
  assert.equal(e.chatLog.children.length,12);
});

test('unchanged saved history keeps message nodes while equal-count edits refresh them',async()=>{
  const history=rows.slice(0,10);
  const {elements:e,browser,chatScroll}=page(()=>null,history);
  await settle();
  const original=e.chatLog.children[0];
  chatScroll.scrollTop=40;
  await browser.emit('online');
  assert.equal(e.chatLog.children[0],original);
  assert.equal(chatScroll.scrollTop,40);
  history[0]={...history[0],content:'edited message'};
  await e.syncHistory.emit('click');
  assert.notEqual(e.chatLog.children[0],original);
  assert.equal(contents(e.chatLog)[0],'edited message');
  const editedNode=e.chatLog.children[0];
  history.unshift({role:'assistant',content:'inserted earlier'});
  await e.syncHistory.emit('click');
  assert.notEqual(e.chatLog.children[1],editedNode);
  assert.deepEqual(contents(e.chatLog),history.map(row=>row.content));
});

test('a local send invalidates the previously rendered history',async()=>{
  const {elements:e}=page();
  await settle();
  e.msgInput.value='unsaved message';
  await e.sendBtn.emit('click');
  assert.match(e.chatLog.textContent,/unsaved message/);
  await e.syncHistory.emit('click');
  assert.deepEqual(contents(e.chatLog),['saved']);
});

test('return to latest is available while reading and jumps without sending a request',async()=>{
  const {elements:e,chatScroll,calls}=page(()=>null,rows);
  await settle();
  assert.equal(e.jumpLatest.hidden,true);
  chatScroll.scrollTop=40;
  await chatScroll.emit('scroll');
  assert.equal(e.jumpLatest.hidden,false);
  e.msgInput.value='draft';
  const callCount=calls.length;
  await e.jumpLatest.emit('click');
  assert.equal(chatScroll.scrollTop,chatScroll.scrollHeight-chatScroll.clientHeight);
  assert.equal(e.jumpLatest.hidden,true);
  assert.equal(chatScroll.focused,true);
  assert.equal(e.msgInput.value,'draft');
  assert.equal(calls.length,callCount);
});

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
  assert.ok(calls.filter(x=>x.url==='/api/me'||x.url.startsWith('/api/conversation?')).every(x=>x.cache==='no-store'));
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

test('quick reconnect aborts an old page read and starts fresh without waiting for it',async()=>{
  let release,hold=false;
  const history=rows.map(row=>({...row}));
  const state=page(url=>{
    if (hold && url.includes('offset=100') && !release) return new Promise(resolve=>release=resolve);
  },history);
  const {elements:e,calls,browser,navigator}=state;
  await settle();
  const original=e.chatLog.children[0];
  hold=true;
  const initialReads=reads(calls).length;
  await e.syncHistory.emit('click');
  const signal=reads(calls).at(-1).signal;
  e.msgInput.value='draft';
  navigator.onLine=false;
  await browser.emit('offline');
  assert.equal(signal.aborted,true);
  assert.equal(e.cancelHistory.hidden,true);
  assert.match(e.connectionStatus.textContent,/网络已断开/);
  assert.equal(e.chatLog.children[0],original);
  assert.deepEqual(contents(e.chatLog),rows.map(row=>row.content));
  const offlineReads=reads(calls).length;
  await e.syncHistory.emit('click');
  assert.equal(reads(calls).length,offlineReads);
  history[0]={...history[0],content:'updated while offline'};
  navigator.onLine=true;
  await browser.emit('online');
  assert.deepEqual(reads(calls).slice(initialReads).map(call=>new URLSearchParams(call.url.split('?')[1]).get('offset')),['0','100','0','100','200']);
  release(json({},401));
  await settle();
  assert.equal(e.reloginLink.hidden,true);
  assert.deepEqual(contents(e.chatLog),history.map(row=>row.content));
  assert.equal(e.msgInput.value,'draft');
  assert.match(e.connectionStatus.textContent,/历史已同步/);
  assert.equal(state.calls.filter(call=>call.method==='POST').length,0);
});

test('quick reconnect bypasses the old retry timer and never repeats its read',async()=>{
  let fail=true;
  const state=page(url=>fail&&url.startsWith('/api/conversation?')?json({},503):null);
  const {elements:e,calls,browser,navigator,timers}=state;
  await settle();
  assert.equal(timers.length,1);
  navigator.onLine=false;
  await browser.emit('offline');
  fail=false;
  navigator.onLine=true;
  await browser.emit('online');
  assert.equal(reads(calls).length,2);
  assert.match(e.connectionStatus.textContent,/历史已同步/);
  const count=calls.length;
  await state.retry();
  assert.equal(calls.length,count);
  assert.match(e.connectionStatus.textContent,/历史已同步/);
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

test('final history failure describes the last attempt and preserves display and draft',async()=>{
  const timeout=Object.assign(new Error('private timeout detail'),{name:'TimeoutError'});
  const service=json({detail:'private service detail'},503);
  const cases=[
    {failures:[service,service,timeout],message:/同步超时/},
    {failures:[timeout,timeout,service],message:/服务暂时异常/},
    {failures:[service,service,json({},422)],message:/历史同步失败/},
  ];
  for (const {failures,message} of cases) {
    let fail=false, attempt=0;
    const state=page(url=>{
      if (!fail || !url.startsWith('/api/conversation?')) return null;
      const result=failures[attempt++];
      if (result instanceof Error) throw result;
      return result;
    });
    const e=state.elements;
    await settle();
    const before=e.chatLog.textContent;
    e.msgInput.value='draft';
    fail=true;
    await e.syncHistory.emit('click');
    await state.retry();
    await state.retry();
    assert.equal(attempt,3);
    assert.match(e.connectionStatus.textContent,message);
    assert.doesNotMatch(e.connectionStatus.textContent,/private|重新登录/);
    assert.equal(e.chatLog.textContent,before);
    assert.equal(e.msgInput.value,'draft');
    assert.equal(e.syncHistory.disabled,false);
    fail=false;
    await e.syncHistory.emit('click');
    assert.match(e.connectionStatus.textContent,/历史已同步/);
  }
});

test('exhausted history changes ask the user to sync later and keep the displayed history',async()=>{
  const firstPage={messages:rows.slice(0,100),total:205,conversation_id:'first',history_revision:'before'};
  const secondPage={...firstPage,messages:rows.slice(100,200)};
  const cases=[
    {...secondPage,total:206},
    {...secondPage,conversation_id:'second'},
    {...secondPage,history_revision:'after'},
  ];
  for (const changedPage of cases) {
    let changing=false;
    let recovered=false;
    const recoveredRows=rows.map(row=>({...row,content:'recovered '+row.content}));
    const state=page(url=>{
      if (!url.startsWith('/api/conversation?')) return null;
      if (recovered) {
        const offset=Number(new URLSearchParams(url.split('?')[1]).get('offset'));
        return json({messages:recoveredRows.slice(offset,offset+100),total:recoveredRows.length,
          conversation_id:'recovered',history_revision:'stable'});
      }
      if (!changing) return null;
      return json(url.includes('offset=0&')?firstPage:changedPage);
    });
    const e=state.elements;
    await settle();
    e.msgInput.value='draft';
    changing=true;
    await e.syncHistory.emit('click');
    await state.retry();
    await state.retry();
    assert.equal(reads(state.calls).length,7);
    assert.equal(state.timers.length,0);
    assert.match(e.connectionStatus.textContent,/历史.*变化.*稍后.*同步历史/);
    assert.deepEqual(contents(e.chatLog),['saved']);
    assert.equal(e.msgInput.value,'draft');
    assert.equal(e.syncHistory.disabled,false);
    assert.equal(e.cancelHistory.hidden,true);
    assert.deepEqual(reads(state.calls).slice(1).map(call=>
      Number(new URLSearchParams(call.url.split('?')[1]).get('offset'))),[0,100,0,100,0,100]);
    changing=false;
    recovered=true;
    await e.syncHistory.emit('click');
    assert.match(e.connectionStatus.textContent,/历史已同步/);
    assert.deepEqual(contents(e.chatLog),recoveredRows.map(row=>row.content));
    assert.equal(e.msgInput.value,'draft');
    assert.equal(state.timers.length,0);
    assert.equal(state.calls.filter(call=>call.method==='POST').length,0);
  }
});

test('history body abort uses the active attempt timeout reason',async()=>{
  let fail=false;
  const state=page(url=>{
    if (!fail || !url.startsWith('/api/conversation?')) return null;
    return {ok:true,json:async()=>{
      Object.assign(state.timeouts.at(-1),{aborted:true,reason:{name:'TimeoutError'}});
      throw Object.assign(new Error('body aborted'),{name:'AbortError'});
    }};
  });
  await settle();
  fail=true;
  await state.elements.syncHistory.emit('click');
  await state.retry();
  await state.retry();
  assert.match(state.elements.connectionStatus.textContent,/同步超时/);
});

test('equal-sized history from another conversation forces a fresh read without mixing pages',async()=>{
  let readsStarted=0;
  const newRows=rows.map(row=>({...row,content:'new-'+row.content}));
  const state=page(url=>{
    if (!url.startsWith('/api/conversation?')) return null;
    const offset=Number(new URLSearchParams(url.split('?')[1]).get('offset'));
    const first=readsStarted++===0;
    const source=first?rows:newRows;
    return json({messages:source.slice(offset,offset+100),total:205,conversation_id:first?'first':'second'});
  });
  await settle();
  state.elements.msgInput.value='draft';
  assert.equal(state.elements.chatLog.textContent,'welcome');
  assert.equal(state.timers.length,1);
  await state.retry();
  assert.deepEqual(reads(state.calls).map(call=>new URLSearchParams(call.url.split('?')[1]).get('offset')),['0','100','0','100','200']);
  assert.deepEqual(contents(state.elements.chatLog),newRows.map(row=>row.content));
  assert.equal(state.elements.msgInput.value,'draft');
});

test('a changed history revision restarts paging even when conversation and count match',async()=>{
  let readsStarted=0;
  const edited=[{...rows[0],content:'edited first message'},...rows.slice(1)];
  const state=page(url=>{
    if (!url.startsWith('/api/conversation?')) return null;
    const offset=Number(new URLSearchParams(url.split('?')[1]).get('offset'));
    const first=readsStarted++===0;
    return json({messages:(first?rows:edited).slice(offset,offset+100),total:205,
      conversation_id:'same',history_revision:first?'before':'after'});
  });
  await settle();
  state.elements.msgInput.value='draft';
  assert.equal(state.elements.chatLog.children.length,0);
  await state.retry();
  assert.equal(readsStarted,5);
  assert.deepEqual(contents(state.elements.chatLog),edited.map(row=>row.content));
  assert.equal(state.elements.msgInput.value,'draft');
});

test('history change shows the next retry during the wait and allows cancellation',async()=>{
  let changing=false;
  const stableRows=rows.map(row=>({...row,content:'stable '+row.content}));
  const state=page(url=>{
    if (!url.startsWith('/api/conversation?')) return null;
    const offset=Number(new URLSearchParams(url.split('?')[1]).get('offset'));
    return json({
      messages:(changing?rows:stableRows).slice(offset,offset+100),
      total:rows.length,
      conversation_id:'same',
      history_revision:changing&&offset===0?'before':'after',
    });
  });
  await settle();
  const e=state.elements;
  e.msgInput.value='draft';
  const original=e.chatLog.children[0];
  changing=true;
  await e.syncHistory.emit('click');
  assert.match(e.connectionStatus.textContent,/历史.*变化.*1 秒后.*从首页.*2\/3/);
  assert.doesNotMatch(e.connectionStatus.textContent,/已读取/);
  await state.retry();
  assert.match(e.connectionStatus.textContent,/历史.*变化.*2 秒后.*从首页.*3\/3/);
  assert.equal(e.cancelHistory.hidden,false);
  await e.cancelHistory.emit('click');
  const count=state.calls.length;
  await state.retry();
  assert.equal(state.calls.length,count);
  assert.match(e.connectionStatus.textContent,/已取消/);
  assert.equal(e.chatLog.children[0],original);
  assert.deepEqual(contents(e.chatLog),stableRows.map(row=>row.content));
  assert.equal(e.msgInput.value,'draft');
  assert.equal(e.syncHistory.disabled,false);
  assert.equal(e.cancelHistory.hidden,true);
  changing=false;
  stableRows[0]={...stableRows[0],content:'updated after cancellation'};
  await e.syncHistory.emit('click');
  assert.match(e.connectionStatus.textContent,/历史已同步/);
  assert.deepEqual(contents(e.chatLog),stableRows.map(row=>row.content));
  assert.equal(e.msgInput.value,'draft');
  assert.deepEqual(reads(state.calls).slice(-3).map(call=>
    Number(new URLSearchParams(call.url.split('?')[1]).get('offset'))),[0,100,200]);
  assert.equal(state.timers.length,0);
  assert.equal(state.calls.filter(call=>call.method==='POST').length,0);
});

test('last page progress waits for final identity before reporting success',async()=>{
  let identities=0,release;
  const {elements:e}=page(url=>url==='/api/me'&&++identities===2?new Promise(r=>release=r):null);
  await settle(); assert.match(e.connectionStatus.textContent,/已读取 1\/1 条.*正在核对身份/);
  assert.equal(e.chatLog.textContent,'welcome');
  release(json({user:null}));await settle();
  assert.match(e.connectionStatus.textContent,/重新登录/);assert.equal(e.chatLog.textContent,'welcome');
});

test('empty history preserves the display, draft and any unresolved send',async()=>{
  for (const uncertain of [false,true]) {
    let empty=false;
    const state=page(url=>{
      if(url==='/api/chat')return json({},503);
      if(empty&&url.startsWith('/api/conversation?'))return json({messages:[],total:0});
    });
    const {elements:e,calls,timers}=state;
    await settle();
    if (uncertain) {
      e.msgInput.value='unconfirmed';
      await e.sendBtn.emit('click');
    }
    const before=e.chatLog.textContent;
    e.msgInput.value='draft';
    empty=true;
    await e.syncHistory.emit('click');
    assert.match(e.connectionStatus.textContent,/未读取到已保存的历史.*保留/);
    if(uncertain){
      assert.match(e.connectionStatus.textContent,/勿重复提交/);
      assert.match(e.uncertainTexts.textContent,/unconfirmed/);
    }
    assert.equal(e.chatLog.textContent,before);
    assert.equal(e.msgInput.value,'draft');
    assert.equal(e.syncHistory.disabled,false);
    assert.equal(e.reloginLink.hidden,true);
    assert.equal(timers.length,0);
    assert.equal(calls.filter(x=>x.method==='POST').length,Number(uncertain));
    empty=false;
    await e.syncHistory.emit('click');
    assert.match(e.connectionStatus.textContent,/已同步/);
  }
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
  assert.equal(timers.length,0); assert.equal(reads(calls).length,1); assert.equal(e.sendBtn.disabled,true);
  assert.equal(e.reloginLink.hidden,false);
});

for (const status of [401,403]) {
  test('HTTP '+status+' during paging stops retries and keeps display',async()=>{
    const {elements:e,calls,timers}=page(url=>url.startsWith('/api/conversation?')?json({},status):null);
    await settle(); assert.equal(e.chatLog.textContent,'welcome'); assert.match(e.connectionStatus.textContent,/重新登录/);
    assert.equal(reads(calls).length,1); assert.equal(timers.length,0); assert.equal(e.syncHistory.disabled,true);
    assert.equal(e.reloginLink.hidden,false);
  });
}

test('expired identity keeps existing display, draft and uncertain text and locks existing writes',async()=>{
  let expired=false, sends=0;
  const {elements:e,calls,browser,fillAndSend}=page(url=>{
    if(url==='/api/me'&&expired)return json({user:null});
    if(url==='/api/chat')return ++sends===1?json({reply:'answer',suggest_ticket:true}):json({},503);
  });
  await settle();assert.equal(e.reloginLink.hidden,true);
  e.msgInput.value='first';await e.sendBtn.emit('click');const confirm=e.chatLog.querySelector('.ct-btn.primary');assert.ok(confirm);
  e.msgInput.value='unconfirmed';await e.sendBtn.emit('click');
  e.msgInput.value='draft';const before=e.chatLog.textContent;expired=true;
  await e.syncHistory.emit('click');
  assert.equal(e.reloginLink.hidden,false);assert.equal(e.sendBtn.disabled,true);assert.equal(confirm.disabled,true);
  const count=calls.length;await e.sendBtn.emit('click');await confirm.emit('click');await browser.emit('online');await browser.emit('offline');
  fillAndSend('suggested question');await settle();
  assert.equal(calls.length,count);assert.match(e.connectionStatus.textContent,/重新登录/);
  assert.equal(e.chatLog.textContent,before);assert.equal(e.msgInput.value,'draft');assert.match(e.uncertainTexts.textContent,/unconfirmed/);
});

test('expired page copies the current draft and each uncertain question without navigation or requests',async()=>{
  let expired=false;
  const {elements:e,calls,browser,navigator}=page(url=>{
    if(url==='/api/me'&&expired)return json({user:null});
    if(url==='/api/chat')return json({},503);
  });
  const copied=[];
  navigator.clipboard={writeText:async text=>copied.push(text)};
  await settle();
  assert.equal(e.copyUnsaved.hidden,true);
  for(const question of ['问题一\n第二行','问题二']){
    e.msgInput.value=question;
    await e.sendBtn.emit('click');
  }
  expired=true;
  await e.syncHistory.emit('click');
  e.msgInput.value='  最新草稿\n保留换行  ';
  assert.equal(e.copyUnsaved.hidden,false);
  const count=calls.length;
  await e.copyUnsaved.emit('click');
  assert.deepEqual(copied,['未发送草稿\n  最新草稿\n保留换行  \n\n发送结果待核对\n问题一\n第二行\n\n问题二']);
  assert.match(e.copyUnsavedStatus.textContent,/已复制/);
  assert.equal(e.msgInput.value,'  最新草稿\n保留换行  ');
  assert.equal(e.uncertainTexts.children.length,2);
  assert.equal(calls.length,count);
  assert.equal(browser.location.href,'/');
});

test('expired page selects a manual copy fallback when clipboard access is denied or unavailable',async()=>{
  for(const clipboard of [undefined,{writeText:async()=>{throw Error('denied');}}]){
    const {elements:e,navigator}=page(url=>url==='/api/me'?json({user:null}):null);
    navigator.clipboard=clipboard;
    await settle();
    e.msgInput.value='草稿';
    await e.copyUnsaved.emit('click');
    assert.equal(e.copyUnsavedText.hidden,false);
    assert.equal(e.copyUnsavedText.value,'未发送草稿\n草稿');
    assert.equal(e.copyUnsavedText.selected,true);
    assert.match(e.copyUnsavedStatus.textContent,/手动复制/);
    assert.match(e.connectionStatus.textContent,/重新登录/);
  }
});

test('expired page with only whitespace does not write to the clipboard',async()=>{
  const {elements:e,navigator}=page(url=>url==='/api/me'?json({user:null}):null);
  let writes=0;
  navigator.clipboard={writeText:async()=>{writes++;}};
  await settle();
  e.msgInput.value=' \n ';
  await e.copyUnsaved.emit('click');
  assert.equal(writes,0);
  assert.equal(e.copyUnsavedText.hidden,true);
  assert.match(e.copyUnsavedStatus.textContent,/没有可复制/);
});

test('editing an expired draft clears the previous copy feedback and manual text',async()=>{
  const {elements:e,navigator}=page(url=>url==='/api/me'?json({user:null}):null);
  const writes=[];
  navigator.clipboard={writeText:async text=>{writes.push(text);throw Error('denied');}};
  await settle();
  e.msgInput.value='旧草稿';
  await e.copyUnsaved.emit('click');
  assert.equal(e.copyUnsavedText.hidden,false);
  e.msgInput.value='新草稿';
  await e.msgInput.emit('input');
  assert.equal(e.copyUnsavedStatus.textContent,'');
  assert.equal(e.copyUnsavedText.hidden,true);
  assert.equal(e.copyUnsavedText.value,'');
  await e.copyUnsaved.emit('click');
  assert.deepEqual(writes,['未发送草稿\n旧草稿','未发送草稿\n新草稿']);
  assert.equal(e.copyUnsavedText.value,'未发送草稿\n新草稿');
});

test('late clipboard completion cannot show feedback for an edited draft',async()=>{
  for (const denied of [false,true]) {
    const {elements:e,navigator}=page(url=>url==='/api/me'?json({user:null}):null);
    let complete;
    navigator.clipboard={writeText:()=>new Promise((resolve,reject)=>{
      complete=denied?()=>reject(Error('denied')):resolve;
    })};
    await settle();
    e.msgInput.value='旧草稿';
    await e.copyUnsaved.emit('click');
    assert.equal(e.copyUnsaved.disabled,true);
    e.msgInput.value='新草稿';
    await e.msgInput.emit('input');
    complete();
    await settle();
    assert.equal(e.copyUnsavedStatus.textContent,'');
    assert.equal(e.copyUnsavedText.hidden,true);
    assert.equal(e.copyUnsavedText.focused,undefined);
    assert.equal(e.copyUnsaved.disabled,false);
  }
});

test('a late uncertain question invalidates the expired page copy feedback',async()=>{
  let sends=0, reply;
  const {elements:e,navigator}=page((url, options)=>{
    if(url==='/api/chat')return ++sends===1?json({reply:'answer',suggest_ticket:true}):new Promise(resolve=>reply=resolve);
    if(url==='/api/tickets' && options.method==='POST')return json({},401);
  });
  const copied=[];
  navigator.clipboard={writeText:async text=>copied.push(text)};
  await settle();
  e.msgInput.value='first';
  await e.sendBtn.emit('click');
  const confirm=e.chatLog.querySelector('.ct-btn.primary');
  e.msgInput.value='待核对原文';
  await e.sendBtn.emit('click');
  await confirm.emit('click');
  e.msgInput.value='草稿';
  await e.copyUnsaved.emit('click');
  assert.match(e.copyUnsavedStatus.textContent,/已复制/);
  reply(json({},503));
  await settle();
  assert.equal(e.copyUnsavedStatus.textContent,'');
  await e.copyUnsaved.emit('click');
  assert.deepEqual(copied,['未发送草稿\n草稿','未发送草稿\n草稿\n\n发送结果待核对\n待核对原文']);
});

test('ticket 401 locks identity and cancels a history request already in flight',async()=>{
  let hold=false,release;
  const {elements:e,calls}=page((url, options)=>{
    if(url==='/api/chat')return json({reply:'answer',suggest_ticket:true});
    if(url==='/api/tickets' && options.method==='POST')return json({},401);
    if(hold&&url.startsWith('/api/conversation?'))return new Promise(r=>release=r);
  });
  await settle();e.msgInput.value='question';await e.sendBtn.emit('click');
  const confirm=e.chatLog.querySelector('.ct-btn.primary');hold=true;await e.syncHistory.emit('click');
  const request=reads(calls).at(-1);await confirm.emit('click');
  assert.equal(request.signal.aborted,true);assert.equal(e.reloginLink.hidden,false);assert.equal(confirm.disabled,true);
  release(json({messages:[{role:'user',content:'late'}],total:1}));await settle();
  assert.doesNotMatch(e.chatLog.textContent,/late/);assert.match(e.connectionStatus.textContent,/重新登录/);
});

test('chat 401 keeps the question and leaves send disabled after the request finishes',async()=>{
  const {elements:e,calls}=page(url=>url==='/api/chat'?json({},401):null);
  await settle();e.msgInput.value='question';await e.sendBtn.emit('click');
  assert.equal(e.reloginLink.hidden,false);assert.equal(e.sendBtn.disabled,true);
  assert.match(e.uncertainTexts.textContent,/question/);assert.match(e.connectionStatus.textContent,/重新登录/);
  e.msgInput.value='draft';await e.sendBtn.emit('click');
  assert.equal(calls.filter(x=>x.method==='POST').length,1);assert.equal(e.msgInput.value,'draft');
});

test('ticket 403 retains the confirmation without classifying it as session expiry',async()=>{
  const {elements:e}=page((url, options)=>url==='/api/chat'?json({reply:'answer',suggest_ticket:true}):url==='/api/tickets' && options.method==='POST'?json({},403):null);
  await settle();e.msgInput.value='question';await e.sendBtn.emit('click');
  const confirm=e.chatLog.querySelector('.ct-btn.primary');await confirm.emit('click');
  assert.equal(e.reloginLink.hidden,true);assert.equal(e.sendBtn.disabled,false);
  assert.equal(confirm.disabled,false);assert.equal(e.chatLog.querySelector('.ct-btn.primary'),confirm);
});

test('exhausted page failures preserve old messages and permit manual sync',async()=>{
  let fail=false; const state=page(url=>fail&&url.startsWith('/api/conversation?')?json({},503):null);
  await settle(); const before=state.elements.chatLog.textContent; fail=true;
  await state.elements.syncHistory.emit('click'); await state.retry(); await state.retry();
  assert.match(state.elements.connectionStatus.textContent,/同步失败/);
  assert.equal(state.elements.chatLog.textContent,before); assert.equal(state.elements.syncHistory.disabled,false);
  fail=false; await state.elements.syncHistory.emit('click');
  assert.match(state.elements.connectionStatus.textContent,/历史已同步/);
});

test('submitting a suggested ticket confirms the draft and refreshes the signed-in ticket list', async () => {
  const ticket = {id: 'T-1', description: '导出失败', status: 'pending_confirm'};
  let stored = false;
  const {elements: e, calls} = page((url, options) => {
    if (url === '/api/chat') return json({reply: '需要人工跟进', suggest_ticket: true});
    if (url === '/api/tickets' && options.method === 'POST') {
      stored = true;
      return json({ticket});
    }
    if (url === '/api/tickets/confirm') {
      assert.deepEqual(JSON.parse(options.body), {ticket_id: 'T-1'});
      ticket.status = 'open';
      return json({ticket});
    }
    if (url === '/api/tickets' && stored) return json({tickets: [ticket], total: 1, page: 1, pages: 1});
  });
  await settle();
  assert.match(e.ticketListStatus.textContent, /暂无工单/);
  e.msgInput.value = ticket.description;
  await e.sendBtn.emit('click');
  await e.chatLog.querySelector('.ct-btn.primary').emit('click');
  assert.match(e.ticketList.textContent, /T-1 · 待处理 · 导出失败/);
  assert.ok(calls.filter(call => call.url.startsWith('/api/tickets'))
    .every(call => call.headers.Authorization === 'Bearer test-token'));
});

test('a saved draft stays visible and can be submitted from my tickets', async () => {
  const ticket = {id: 'T-2', description: '激活失败', status: 'pending_confirm'};
  const {elements: e} = page(url => {
    if (url === '/api/tickets/confirm') {
      ticket.status = 'open';
      return json({ticket});
    }
    if (url === '/api/tickets') return json({tickets: [ticket], total: 1, page: 1, pages: 1});
  });
  await settle();
  assert.match(e.ticketList.textContent, /待确认/);
  await e.ticketList.children[0].children[2].emit('click');
  assert.match(e.ticketList.textContent, /待处理/);
});

test('creating and switching conversations isolates history, drafts and newly sent messages', async () => {
  const conversations = [{id: 'first', title: '问题一'}];
  const histories = {first: [{role: 'user', content: '第一条会话原文'}], second: []};
  const {elements: e, calls} = page((url, options) => {
    if (url === '/api/conversations' && options.method === 'POST') {
      const item = {id: 'second', title: ''};
      conversations.unshift(item);
      return json(item, 201);
    }
    if (url === '/api/conversations') return json({conversations});
    if (url.startsWith('/api/conversation?')) {
      const id = new URLSearchParams(url.split('?')[1]).get('conversation_id');
      return json({messages: histories[id], total: histories[id].length, conversation_id: id});
    }
  });
  await settle();
  assert.match(e.chatLog.textContent, /第一条会话原文/);
  e.msgInput.value = '第一条会话草稿';
  await e.createConversation.emit('click');
  assert.doesNotMatch(e.chatLog.textContent, /第一条会话原文/);
  assert.equal(e.msgInput.value, '');
  e.msgInput.value = '第二条会话的新问题';
  await e.sendBtn.emit('click');
  const sent = calls.find(call => call.url === '/api/chat');
  assert.deepEqual(JSON.parse(sent.body), {message: '第二条会话的新问题', conversation_id: 'second'});
  await e.conversationList.children[1].emit('click');
  assert.match(e.chatLog.textContent, /第一条会话原文/);
  assert.doesNotMatch(e.chatLog.textContent, /第二条会话的新问题/);
  assert.equal(e.msgInput.value, '第一条会话草稿');
});

test('customer can open a ticket and read its description, evidence and replies', async () => {
  const ticket = {id: 'T-20', status: 'open', description: '无法导出', evidence: '导出指南',
    reply_timeline: {items: [{replier: '客服', content: '已收到'}]}};
  const {elements: e, calls} = page(url => {
    if (url === '/api/tickets') return json({tickets: [ticket], total: 1, pages: 1});
    if (url === '/api/tickets/T-20') return json({ticket});
  });
  await settle();
  await e.ticketList.children[0].children[1].emit('click');
  assert.equal(e.ticketDetail.hidden, false);
  assert.match(e.ticketDetail.textContent, /无法导出/);
  assert.match(e.ticketDetail.textContent, /导出指南/);
  assert.match(e.ticketDetail.textContent, /客服：已收到/);
  assert.equal(calls.find(call => call.url === '/api/tickets/T-20').headers.Authorization, 'Bearer test-token');
});

test('ticket cards reload per conversation and open detail inline', async () => {
  const ticket = {id: 'T-21', status: 'open', description: '无法导出', evidence: '导出指南'};
  const {elements: e} = page((url) => {
    if (url === '/api/conversations') return json({conversations: [{id: 'a', title: '导出'}, {id: 'b', title: '退款'}]});
    if (url === '/api/tickets?conversation_id=a&page=1') return json({tickets: [ticket]});
    if (url === '/api/tickets?conversation_id=b&page=1') return json({tickets: []});
    if (url === '/api/tickets/T-21') return json({ticket});
  });
  await settle();
  assert.match(e.conversationTickets.textContent, /T-21.*待处理.*无法导出/);
  const card = e.conversationTickets.children[0];
  await card.children[2].children[0].emit('click');
  assert.match(card.children[3].textContent, /导出指南/);
  await e.conversationList.children[1].emit('click');
  assert.equal(e.conversationTickets.hidden, true);
  assert.doesNotMatch(e.conversationTickets.textContent, /T-21/);
  await e.conversationList.children[0].emit('click');
  assert.match(e.conversationTickets.textContent, /T-21/);
});
