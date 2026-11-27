'use strict';
class KnowledgeIngest {
  constructor(options) { Object.assign(this, options); this.busy = false; }
  async submit(input) {
    if (this.busy) return;
    const file = input.file;
    const title = input.title.trim(), content = input.content.trim();
    const keywords = [...new Set(input.keywords.split(/[,，\n]/).map(x => x.trim()).filter(Boolean))];
    if (file && (!/\.(pdf|md|markdown)$/i.test(file.name) || file.size === 0 || file.size > 8 * 1024 * 1024)) {
      this.onStatus('请选择非空的 PDF 或 Markdown 文件，大小不能超过 8 MiB。');
      return;
    }
    if ((!title && !file) || input.title.length > 200 || (!file && (!content || input.content.length > 100000)) ||
        !['manual', 'faq', 'rule'].includes(input.kind) || keywords.length > 20) {
      this.onStatus('请填写标题（1–200 字符）和正文（1–100000 字符），选择类型，关键词最多 20 个。');
      return;
    }
    this.busy = true; this.onBusy(true);
    this.onStatus(file ? '正在提取文件正文并入库，扫描页识别可能需要一些时间…' : '正在提交资料，请勿重复操作…');
    try {
      let data;
      if (file) {
        const params = new URLSearchParams({filename: file.name, title, kind: input.kind});
        for (const keyword of keywords) params.append('keywords', keyword);
        data = await this.request('/api/knowledge/ingest/file?' + params, {
          method: 'POST', body: file, headers: {'Content-Type': 'application/octet-stream'},
        });
      } else {
        data = await this.request('/api/knowledge/ingest', {
          method: 'POST', body: JSON.stringify({title, content, kind: input.kind, keywords}),
        });
      }
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
