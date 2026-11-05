'use strict';
class KnowledgeIngest {
  constructor(options) { Object.assign(this, options); this.busy = false; }
  async submit(input) {
    if (this.busy) return;
    const title = input.title.trim(), content = input.content.trim();
    const keywords = [...new Set(input.keywords.split(/[,，\n]/).map(x => x.trim()).filter(Boolean))];
    if (!title || input.title.length > 200 || !content || input.content.length > 100000 ||
        !['manual', 'faq', 'rule'].includes(input.kind) || keywords.length > 20) {
      this.onStatus('请填写标题（1–200 字符）和正文（1–100000 字符），选择类型，关键词最多 20 个。');
      return;
    }
    this.busy = true; this.onBusy(true); this.onStatus('正在提交资料，请勿重复操作…');
    try {
      const data = await this.request('/api/knowledge/ingest', {
        method: 'POST', body: JSON.stringify({title, content, kind: input.kind, keywords}),
      });
      this.onStatus(data.created ? `已新增资料，生成 ${data.chunks} 个分块。编号：${data.knowledge_id}` :
        `资料已存在，本次未新增。编号：${data.knowledge_id}`);
      this.onSaved(data);
    } catch (error) {
      this.onStatus(`${error.message} 输入已保留。连接中断或超时后请先核对列表，再决定是否重试。`);
    } finally {
      this.busy = false; this.onBusy(false);
    }
  }
}
if (typeof module !== 'undefined') module.exports = {KnowledgeIngest};
