'use strict';
class KnowledgeRecovery {
  constructor(options) {
    Object.assign(this, options); this.items = []; this.loading = false; this.recovering = false; this.version = 0;
    this.offset = 0;
    this.limit = 20;
    this.total = 0;
    this.recoveredId = null;
  }
  clear() {
    ++this.version; this.items = []; this.loading = false; this.onRows([]);
    this.offset = 0;
    this.total = 0;
    this.recoveredId = null;
    this.onBusy(this.recovering);
    this.onStatus('待恢复记录已清空，请重新登录后读取。'); this.onResult('');
  }
  async move(direction) {
    const offset = this.offset + direction * this.limit;
    if (this.loading || this.recovering || offset < 0 || offset >= this.total) return;
    await this.load(offset);
  }
  async load(offset = this.offset) {
    if (this.recovering) return;
    const version = ++this.version;
    this.total = 0;
    this.loading = true; this.items = []; this.onRows([]); this.onBusy(true);
    this.onStatus('正在读取待恢复记录…');
    const readPage = start => this.request(`/api/knowledge/ingest/pending?offset=${start}&limit=${this.limit}`);
    try {
      let data = await readPage(offset);
      if (version !== this.version) return;
      if (offset > 0 && data.items.length === 0) {
        offset = 0;
        if (data.total > 0) data = await readPage(offset);
        if (version !== this.version) return;
      }
      this.offset = offset;
      this.total = data.total;
      this.items = data.items; this.onRows(this.items);
      this.onStatus(data.items.length ? `共 ${data.total} 条 · 当前 ${offset + 1}–${offset + data.items.length} 条` : '没有待恢复的入库记录。');
    } catch (error) {
      if (version === this.version) this.onStatus(`${error.message} 请点击刷新重试。`);
    } finally {
      if (version === this.version) { this.loading = false; this.onBusy(false); }
    }
  }
  async recover(fingerprint) {
    if (this.loading || this.recovering || !this.items.some(x => x.fingerprint === fingerprint)) return;
    const version = this.version;
    this.recoveredId = null;
    this.recovering = true; this.onBusy(true); this.onResult('正在恢复，请勿重复操作…');
    let completed = false;
    try {
      const data = await this.request(`/api/knowledge/ingest/recover/${encodeURIComponent(fingerprint)}`, {method: 'POST'});
      if (version !== this.version) return;
      this.recoveredId = data.knowledge_id;
      this.onResult(data.created ? `恢复完成，资料编号：${data.knowledge_id}` : `资料已完成，无需重复写入。编号：${data.knowledge_id}`);
      this.onRecovered(data); completed = true;
    } catch (error) {
      if (version === this.version) {
        this.onResult(`${error.message} 请先刷新待恢复列表核对结果，再决定是否重试；不会自动重发。`);
      }
    } finally {
      this.recovering = false; this.onBusy(false);
    }
    if (completed) await this.load();
  }
}
if (typeof module !== 'undefined') module.exports = {KnowledgeRecovery};
