'use strict';
class KnowledgeRecovery {
  constructor(options) {
    Object.assign(this, options); this.items = []; this.loading = false; this.recovering = false; this.version = 0;
  }
  clear() {
    ++this.version; this.items = []; this.loading = false; this.onRows([]);
    this.onStatus('待恢复记录已清空，请重新登录后读取。'); this.onResult('');
  }
  async load() {
    if (this.recovering) return;
    const version = ++this.version;
    this.loading = true; this.items = []; this.onRows([]); this.onBusy(true);
    this.onStatus('正在读取待恢复记录…');
    try {
      const data = await this.request('/api/knowledge/ingest/pending');
      if (version !== this.version) return;
      this.items = data.items; this.onRows(this.items);
      this.onStatus(data.items.length ? `显示 ${data.items.length} 条待恢复记录（最多 100 条，可能仍在执行）。` : '没有待恢复的入库记录。');
    } catch (error) {
      if (version === this.version) this.onStatus(`${error.message} 请点击刷新重试。`);
    } finally {
      if (version === this.version) { this.loading = false; this.onBusy(false); }
    }
  }
  async recover(fingerprint) {
    if (this.loading || this.recovering || !this.items.some(x => x.fingerprint === fingerprint)) return;
    this.recovering = true; this.onBusy(true); this.onResult('正在恢复，请勿重复操作…');
    let completed = false;
    try {
      const data = await this.request(`/api/knowledge/ingest/recover/${encodeURIComponent(fingerprint)}`, {method: 'POST'});
      this.onResult(data.created ? `恢复完成，资料编号：${data.knowledge_id}` : `资料已完成，无需重复写入。编号：${data.knowledge_id}`);
      this.onRecovered(data); completed = true;
    } catch (error) {
      this.onResult(`${error.message} 请先刷新待恢复列表核对结果，再决定是否重试；不会自动重发。`);
    } finally {
      this.recovering = false; this.onBusy(false);
    }
    if (completed) await this.load();
  }
}
if (typeof module !== 'undefined') module.exports = {KnowledgeRecovery};
