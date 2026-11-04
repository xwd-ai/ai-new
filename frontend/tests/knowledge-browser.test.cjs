const {test} = require('node:test');
const assert = require('node:assert/strict');
const {KnowledgeBrowser} = require('../knowledge-browser.js');

function setup(request) {
  const lists=[], details=[], statuses=[];
  const browser=new KnowledgeBrowser({request, onList:x=>lists.push(x), onDetail:x=>details.push(x), onStatus:x=>statuses.push(x)});
  return {browser,lists,details,statuses};
}
const page=(total,id='a')=>({total,items:total?[{id,title:id,kind:'faq'}]:[]});

test('filters are encoded and reset pagination; navigation uses applied filters', async()=>{
  const urls=[]; const {browser}=setup(async url=>{urls.push(url); return page(45);});
  await browser.search('导出 & FAQ','faq');
  await browser.move(1);
  assert.equal(new URLSearchParams(urls[1].split('?')[1]).get('title'),'导出 & FAQ');
  assert.equal(new URLSearchParams(urls[1].split('?')[1]).get('offset'),'20');
  await browser.search('规则','rule');
  assert.equal(browser.offset,0);
  assert.equal(new URLSearchParams(urls[2].split('?')[1]).get('kind'),'rule');
});
test('older list responses cannot replace a newer search', async()=>{
  const pending=[]; const {browser,lists}=setup(()=>new Promise(resolve=>pending.push(resolve)));
  const first=browser.search('old',''); const second=browser.search('new','faq');
  pending[1](page(1,'new')); await second; pending[0](page(3,'old')); await first;
  assert.equal(lists.at(-1).items[0].id,'new'); assert.equal(browser.total,1);
});
test('failed and empty lists clear results and disable navigation', async()=>{
  let fail=false; const {browser,lists,statuses}=setup(async()=>{if(fail)throw Error('网络连接失败'); return page(0);});
  await browser.search('',''); assert.equal(browser.total,0);
  await browser.move(1); assert.equal(browser.offset,0);
  fail=true; await browser.search('x','');
  assert.deepEqual(lists.at(-1).items,[]); assert.match(statuses.at(-1),/网络连接失败/);
});
test('changing list or closing details invalidates pending source response', async()=>{
  let resolve; const {browser,details}=setup(url=>url.startsWith('/api/sources/')?new Promise(r=>resolve=r):Promise.resolve(page(1)));
  const pending=browser.source('id/with space');
  await browser.search('new',''); resolve({title:'old',content:'old'}); await pending;
  assert.equal(details.at(-1),null);
  const second=browser.source('a'); browser.closeDetail(); resolve({title:'stale'}); await second;
  assert.equal(details.at(-1),null);
});
test('source IDs are encoded and source failure remains recoverable', async()=>{
  let url; const {browser,details}=setup(async path=>{url=path; throw Error('资料不存在');});
  await browser.source('id/with space');
  assert.equal(url,'/api/sources/id%2Fwith%20space'); assert.equal(details.at(-1).error,'资料不存在');
});
