'use strict';
class KnowledgeBrowser {
  constructor({request, onList, onDetail, onStatus}) {
    Object.assign(this, {request, onList, onDetail, onStatus});
    this.offset = 0; this.total = 0; this.limit = 20;
    this.filters = {}; this.listVersion = 0; this.detailVersion = 0; this.busy = false;
  }
  async search(title, kind) {
    this.filters = {title: title.trim(), kind};
    this.offset = 0;
    await this.load();
  }
  async move(direction) {
    const offset = this.offset + direction * this.limit;
    if (this.busy || offset < 0 || offset >= this.total) return;
    this.offset = offset;
    await this.load();
  }
  async load() {
    const version = ++this.listVersion;
    this.closeDetail(); this.busy = true; this.total = 0;
    this.onList({items: [], total: 0}); this.onStatus('正在加载资料…');
    const params = new URLSearchParams({offset: this.offset, limit: this.limit});
    for (const [key, value] of Object.entries(this.filters)) if (value) params.set(key, value);
    try {
      const data = await this.request(`/api/knowledge/items?${params}`);
      if (version !== this.listVersion) return;
      this.total = data.total;
      this.busy = false; this.onList(data);
      this.onStatus(data.items.length ? `共 ${data.total} 条 · 当前 ${this.offset + 1}–${this.offset + data.items.length} 条` :
        (data.total ? '当前页已无资料，请重新筛选返回首页。' : '没有匹配资料，请调整标题或类型后重试。'));
    } catch (error) {
      if (version !== this.listVersion) return;
      this.busy = false; this.onList({items: [], total: 0}); this.onStatus(error.message);
    }
  }
  closeDetail() { ++this.detailVersion; this.onDetail(null); }
  async source(id) {
    const version = ++this.detailVersion;
    this.onDetail({title: '正在加载原文…', content: ''});
    try {
      const data = await this.request(`/api/sources/${encodeURIComponent(id)}`);
      if (version === this.detailVersion) this.onDetail(data);
    } catch (error) {
      if (version === this.detailVersion) this.onDetail({error: error.message});
    }
  }
}
if (typeof module !== 'undefined') module.exports = {KnowledgeBrowser};

if (typeof document !== 'undefined') {
  const byId = id => document.getElementById(id);
  const token = localStorage.getItem('token') || '';
  let authorized = false;
  async function request(path, options = {}) {
    if (!authorized && path !== '/api/me') throw new Error('登录已失效，请重新登录。');
    let response;
    try {
      response = await fetch(path, {...options,
        headers: {Authorization: `Bearer ${token}`, 'Content-Type': 'application/json'},
        signal: AbortSignal.timeout(options.method === 'POST' ? 60000 : 20000)});
      if (response.status === 401 || response.status === 403) {
        authorized = false;
        byId('filters').disabled = true;
        byId('ingestFields').disabled = true;
        savedId = null; byId('viewSaved').hidden = true;
        pending.clear();
        byId('previous').disabled = byId('next').disabled = true;
        byId('rows').replaceChildren();
        browser.closeDetail();
        byId('status').textContent = '登录已失效，请重新登录后加载资料。';
        byId('loginLink').hidden = false;
        throw new Error('登录已失效或没有管理员权限，请重新登录。');
      }
      if (response.status === 409) throw new Error('相同资料正在处理或租约已失效，请稍后核对并重试。');
      const recovering = path.startsWith('/api/knowledge/ingest/recover/');
      if (!response.ok) throw new Error(response.status === 404 ?
        (recovering ? '待恢复记录不存在，请刷新列表。' : '资料不存在，请刷新列表。') :
        response.status === 422 && recovering ? '持久化记录无法恢复，请人工核对输入与处理阶段。' :
        response.status === 422 && options.method === 'POST' ? '入库参数不合法，请检查标题、正文、类型与关键词。' :
        response.status === 422 ? '筛选参数不合法，请检查标题和类型。' : '服务暂时不可用，请重新筛选或稍后重试。');
      return await response.json();
    } catch (error) {
      if (error.name === 'TimeoutError' || error.name === 'AbortError') throw new Error(options.method === 'POST' ?
        '入库请求超时，保存结果尚未确认。' : '请求超时，请重新筛选或重试查看原文。');
      if (error instanceof TypeError) throw new Error('网络连接失败，请检查连接后重试。');
      throw error;
    }
  }
  const browser = new KnowledgeBrowser({request,
    onStatus: message => { byId('status').textContent = authorized ? message : '登录已失效，请重新登录后加载资料。'; },
    onList: data => {
      if (!authorized) data = {items: [], total: 0};
      byId('rows').replaceChildren();
      for (const item of data.items) {
        const row = document.createElement('tr');
        for (const value of [item.title, {manual:'手册', faq:'FAQ', rule:'规则'}[item.kind] || item.kind, item.id]) {
          const cell = document.createElement('td'); cell.textContent = value; row.append(cell);
        }
        const cell = document.createElement('td');
        const button = document.createElement('button'); button.textContent = '查看原文';
        button.addEventListener('click', () => browser.source(item.id)); cell.append(button); row.append(cell);
        byId('rows').append(row);
      }
      byId('previous').disabled = !authorized || browser.busy || browser.offset === 0;
      byId('next').disabled = !authorized || browser.busy || browser.offset + browser.limit >= data.total;
    },
    onDetail: data => {
      byId('detail').hidden = !data;
      byId('detailTitle').textContent = data ? data.error || data.title : '';
      byId('content').textContent = data && data.content || '';
      byId('keywords').textContent = data && data.keywords ? `关键词：${data.keywords.join('、')}` : '';
    },
  });
  byId('searchForm').addEventListener('submit', event => {
    event.preventDefault();
    if (authorized) browser.search(byId('title').value, byId('kind').value);
  });
  byId('previous').addEventListener('click', () => browser.move(-1));
  byId('next').addEventListener('click', () => browser.move(1));
  byId('closeDetail').addEventListener('click', () => browser.closeDetail());
  let savedId = null;
  const ingest = new KnowledgeIngest({request,
    onStatus: message => { byId('ingestStatus').textContent = authorized ? message : '登录已失效，请重新登录。输入仍保留在当前页面。'; },
    onBusy: busy => {
      byId('ingestFields').disabled = !authorized || busy;
      if (busy) { savedId = null; byId('viewSaved').hidden = true; }
    },
    onSaved: data => {
      if (!authorized) return;
      savedId = data.knowledge_id; byId('viewSaved').hidden = false;
      browser.load();
    },
  });
  byId('ingestForm').addEventListener('submit', event => {
    event.preventDefault();
    if (authorized) ingest.submit({title: byId('ingestTitle').value, content: byId('ingestContent').value,
      kind: byId('ingestKind').value, keywords: byId('ingestKeywords').value});
  });
  byId('ingestForm').addEventListener('input', () => {
    savedId = null; byId('viewSaved').hidden = true;
  });
  byId('viewSaved').addEventListener('click', () => {
    if (authorized && savedId) browser.source(savedId);
  });
  const pending = new KnowledgeRecovery({request,
    onStatus: message => { byId('pendingStatus').textContent = authorized ? message : '登录已失效，请重新登录后读取待恢复记录。'; },
    onResult: message => {
      byId('recoveryStatus').textContent = authorized ? message : '';
      byId('viewRecovered').hidden = !authorized || !pending.recoveredId;
    },
    onRecovered: () => { if (authorized) browser.load(); },
    onBusy: busy => {
      byId('refreshPending').disabled = !authorized || busy;
      byId('pendingStage').disabled = !authorized || pending.recovering;
      byId('pendingStage').value = pending.stage;
      byId('pendingPrevious').disabled = !authorized || busy || pending.offset === 0 || pending.total === 0;
      byId('pendingNext').disabled = !authorized || busy || pending.offset + pending.limit >= pending.total;
      for (const button of byId('pendingRows').querySelectorAll('button')) button.disabled = !authorized || busy;
    },
    onRows: items => {
      byId('pendingRows').replaceChildren();
      if (!authorized) return;
      for (const item of items) {
        const row = document.createElement('tr');
        const stage = {parsed: '已保存输入', chunked: '已完成分块'}[item.stage] || item.stage;
        const date = new Date(item.updated_at * 1000);
        for (const value of [item.knowledge_id, stage, item.chunk_count,
          Number.isNaN(date.getTime()) ? '未知时间' : date.toLocaleString()]) {
          const cell = document.createElement('td'); cell.textContent = value; row.append(cell);
        }
        const cell = document.createElement('td'), button = document.createElement('button');
        button.textContent = '恢复入库';
        button.addEventListener('click', () => { if (authorized) pending.recover(item.fingerprint); });
        cell.append(button); row.append(cell); byId('pendingRows').append(row);
      }
    },
  });
  byId('refreshPending').addEventListener('click', () => { if (authorized) pending.load(0); });
  byId('pendingStage').addEventListener('change', () => {
    if (authorized) pending.selectStage(byId('pendingStage').value);
  });
  byId('pendingPrevious').addEventListener('click', () => { if (authorized) pending.move(-1); });
  byId('pendingNext').addEventListener('click', () => { if (authorized) pending.move(1); });
  byId('viewRecovered').addEventListener('click', () => {
    if (authorized && pending.recoveredId) {
      browser.source(pending.recoveredId);
      byId('detail').focus();
    }
  });
  (async () => {
    try {
      const identity = await request('/api/me');
      if (!identity.user || identity.user.role !== 'admin') throw new Error('仅管理员可以浏览资料列表，请登录管理员账号。');
      authorized = true; byId('filters').disabled = byId('ingestFields').disabled = false;
      byId('refreshPending').disabled = false;
      byId('pendingStage').disabled = false;
      byId('access').textContent = `管理员：${identity.user.name || identity.user.username}`;
      await browser.search('', '');
    } catch (error) {
      byId('access').textContent = error.message; byId('loginLink').hidden = false;
    }
  })();
}
