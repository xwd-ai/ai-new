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
  async function request(path) {
    let response;
    try {
      response = await fetch(path, {headers: {Authorization: `Bearer ${token}`}, signal: AbortSignal.timeout(20000)});
      if (response.status === 401 || response.status === 403) {
        authorized = false;
        byId('filters').disabled = true;
        byId('previous').disabled = byId('next').disabled = true;
        byId('rows').replaceChildren();
        byId('loginLink').hidden = false;
        throw new Error('登录已失效或没有管理员权限，请重新登录。');
      }
      if (!response.ok) throw new Error(response.status === 404 ? '资料不存在，请刷新列表。' :
        response.status === 422 ? '筛选参数不合法，请检查标题和类型。' : '服务暂时不可用，请重新筛选或稍后重试。');
      return await response.json();
    } catch (error) {
      if (error.name === 'TimeoutError' || error.name === 'AbortError') throw new Error('请求超时，请重新筛选或重试查看原文。');
      if (error instanceof TypeError) throw new Error('网络连接失败，请检查连接后重试。');
      throw error;
    }
  }
  const browser = new KnowledgeBrowser({request,
    onStatus: message => { byId('status').textContent = message; },
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
  (async () => {
    try {
      const identity = await request('/api/me');
      if (!identity.user || identity.user.role !== 'admin') throw new Error('仅管理员可以浏览资料列表，请登录管理员账号。');
      authorized = true; byId('filters').disabled = false;
      byId('access').textContent = `管理员：${identity.user.name || identity.user.username}`;
      await browser.search('', '');
    } catch (error) {
      byId('access').textContent = error.message; byId('loginLink').hidden = false;
    }
  })();
}
