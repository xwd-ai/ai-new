const {test} = require('node:test');
const assert = require('node:assert/strict');
const {ChatRecovery} = require('../chat-recovery.js');
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
