const {test} = require('node:test');
const assert = require('node:assert/strict');
const {ChatRecovery} = require('../chat-recovery.js');
const {fetchPagedHistory} = require('../chat-recovery.js');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync(require('node:path').join(__dirname, '../index.html'), 'utf8');

test('page send timeout releases controls and preserves uncertain question outside history', async () => {
  const match = html.match(/(async function handleSend\(\) \{[\s\S]*?)\n    function autoResize/);
  const retained = [], detail = {hidden:true};
  let requestOptions, timeout;
  const {recovery} = setup(async () => []);
  const context = {
    input: {value:'尚未确认保存的问题', style:{}}, recovery, sendBtn:{}, token:'test',
    addUserMsg() {}, addAiContainer: () => ({}), addTyping: () => ({style:{}}),
    addTrace() {}, addSources() {}, addConfirmTicket() {}, scrollBottom() {},
    AbortSignal: {timeout: ms => {timeout = ms; return 'bounded-signal';}},
    fetch: async (url, options) => {requestOptions=options; throw new Error('timeout');},
    document: {createElement: () => ({}), getElementById: id => id === 'uncertainTexts'
      ? {appendChild: item => retained.push(item.textContent)} : detail},
  };
  const handleSend = vm.runInNewContext('(' + match[1].trim() + ')', context);
  await handleSend();
  assert.equal(requestOptions.signal, 'bounded-signal');
  assert.equal(timeout, 60000);
  assert.equal(recovery.sending, false);
  assert.deepEqual(retained, ['尚未确认保存的问题']);
  assert.equal(detail.hidden, false);
  await recovery.sync();
  assert.equal(retained.length, 1);
});

function setup(fetchHistory, options = {}) {
  const messages = [], statuses = [], busy = [];
  const recovery = new ChatRecovery({fetchHistory, onMessages: x => messages.push(x),
    onStatus: x => statuses.push(x), onBusy: x => busy.push(x),
    sleep: async () => {}, isOnline: () => true, ...options});
  return {recovery, messages, statuses, busy};
}

test('network failure retries history reads and renders only once', async () => {
  let calls = 0;
  const {recovery, messages, busy} = setup(async () => {
    if (++calls < 3) throw new TypeError('network');
    return [{role: 'assistant', content: '已保存回答'}];
  });
  await recovery.sync();
  assert.equal(calls, 3);
  assert.equal(messages.length, 1);
  assert.deepEqual(busy, [true, false]);
});

test('retries stop after three attempts and permit manual retry', async () => {
  let calls = 0;
  const {recovery, statuses} = setup(async () => { calls++; throw new Error('unavailable'); });
  await recovery.sync();
  assert.equal(calls, 3);
  assert.match(statuses.at(-1), /同步失败/);
  await recovery.sync();
  assert.equal(calls, 6);
});

test('offline does not fetch or send', async () => {
  let calls = 0;
  const {recovery} = setup(async () => {calls++; return [];}, {isOnline: () => false});
  await recovery.sync();
  assert.equal(calls, 0);
  assert.equal(recovery.beginSend(), false);
});

test('history and sends cannot overlap, duplicate sends are rejected', async () => {
  let resolve;
  const {recovery} = setup(() => new Promise(r => {resolve = r;}));
  const syncing = recovery.sync();
  assert.equal(recovery.beginSend(), false);
  resolve([]);
  await syncing;
  assert.equal(recovery.beginSend(), true);
  assert.equal(recovery.beginSend(), false);
  recovery.finishSend(false);
  assert.equal(recovery.beginSend(), true);
});

test('online sync during send is deferred until the send completes', async () => {
  let calls = 0;
  const {recovery} = setup(async () => {calls++; return [];});
  recovery.beginSend();
  await recovery.sync();
  assert.equal(calls, 0);
  await recovery.finishSend(false);
  assert.equal(calls, 1);
});

test('uncertain send triggers no automatic resubmission and remains explicit', async () => {
  let reads = 0;
  const {recovery, statuses} = setup(async () => {reads++; return [];});
  recovery.beginSend();
  await recovery.finishSend(true);
  assert.equal(reads, 0);
  assert.match(statuses.at(-1), /不确定/);
  await recovery.sync();
  assert.equal(reads, 1);
  assert.match(statuses.at(-1), /勿重复提交/);
});

test('authentication failures are not repeatedly retried', async () => {
  let calls = 0;
  const {recovery, statuses} = setup(async () => {calls++; throw Object.assign(new Error('请重新登录'), {retryable:false});});
  await recovery.sync();
  assert.equal(calls, 1);
  assert.match(statuses.at(-1), /重新登录/);
});

test('concurrent history refreshes do not append duplicate messages', async () => {
  let resolve, calls = 0;
  const {recovery, messages} = setup(() => {calls++; return new Promise(r => {resolve = r;});});
  const first = recovery.sync();
  await recovery.sync();
  resolve([{role:'user', content:'问题'}]);
  await first;
  assert.equal(calls, 1);
  assert.equal(messages.length, 1);
});

const json = data => ({ok:true, json:async () => data});
test('page history callback shares one ten second signal for identity and pages', async () => {
  const source = html.replace(/\r\n/g,'\n');
  const start = source.indexOf('fetchHistory: async onProgress => {');
  const end = source.indexOf('\n      },\n      onMessages:',start);
  const code = source.slice(start+'fetchHistory: '.length,end)+'\n      }';
  const options = [], timeouts = [], signal = {};
  const callback = vm.runInNewContext('('+code+')', {
    token:'test', me:{id:'u'}, fetchPagedHistory,
    AbortSignal:{timeout:ms=>{timeouts.push(ms);return signal;}},
    fetch:async (path,option)=>{options.push(option);return json(path==='/api/me'?{user:{id:'u'}}:{messages:[],total:0});},
  });
  assert.deepEqual(await callback(),[]);
  assert.deepEqual(timeouts,[10000]); assert.equal(options.length,3);
  assert.ok(options.every(option=>option===options[0] && option.signal===signal));
});

test('paged history reads in order and checks identity before and after', async () => {
  const calls = [], progress = [], rows = Array.from({length:205}, (_,i) => ({role:'user',content:String(i)}));
  const result = await fetchPagedHistory(async path => {
    calls.push(path);
    if (path === '/api/me') return json({user:{id:'u'}});
    const offset = Number(new URLSearchParams(path.split('?')[1]).get('offset'));
    return json({messages:rows.slice(offset,offset+100),total:205});
  }, 'u', (loaded,total)=>progress.push([loaded,total]));
  assert.deepEqual(progress,[[100,205],[200,205],[205,205]]);
  assert.deepEqual(result,rows);
  assert.deepEqual(calls,['/api/me','/api/conversation?offset=0&limit=100',
    '/api/conversation?offset=100&limit=100','/api/conversation?offset=200&limit=100','/api/me']);
});

test('failed later page restarts complete read and never renders a partial history', async () => {
  let fail = true; const offsets = [];
  const {recovery,messages} = setup(() => fetchPagedHistory(async path => {
    if (path === '/api/me') return json({user:{id:'u'}});
    const offset = Number(new URLSearchParams(path.split('?')[1]).get('offset')); offsets.push(offset);
    if (offset === 100 && fail) { fail=false; throw Error('network'); }
    return json({messages:Array.from({length:offset ? 1:100},()=>({role:'user',content:'saved'})),total:101});
  },'u'));
  await recovery.sync();
  assert.deepEqual(offsets,[0,100,0,100]);
  assert.equal(messages.length,1); assert.equal(messages[0].length,101);
});

test('expiry at final identity check stops retries and does not replace history', async () => {
  let identities=0;
  const {recovery,messages,statuses} = setup(() => fetchPagedHistory(async path => {
    if (path === '/api/me') return json({user:++identities===1 ? {id:'u'}:null});
    return json({messages:[],total:0});
  },'u'));
  await recovery.sync();
  assert.equal(identities,2); assert.deepEqual(messages,[]); assert.match(statuses.at(-1),/重新登录/);
});

for (const data of [{messages:[],total:1},{messages:[],total:-1},{messages:[]},
  {messages:[],total:0.5}]) {
  test('invalid page rejected: '+JSON.stringify(data),async()=>{
    await assert.rejects(fetchPagedHistory(async path => json(path === '/api/me' ? {user:{id:'u'}}:data),'u'));
  });
}

test('changed total on a later page is rejected', async () => {
  let page=0; const progress=[];
  await assert.rejects(fetchPagedHistory(async path => path === '/api/me' ? json({user:{id:'u'}})
    : json({messages:Array(100).fill({}),total:++page===1 ? 101:102}),'u',(loaded,total)=>progress.push([loaded,total])),/变化/);
  assert.deepEqual(progress,[[100,101]]);
});
