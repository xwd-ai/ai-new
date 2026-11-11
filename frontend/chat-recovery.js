/* HTTP 历史恢复只重试读取，绝不自动重发对话 POST。 */
(function (root) {
  'use strict';
  async function fetchPagedHistory(request, userId, onProgress = () => {}) {
    const read = async path => {
      const response = await request(path);
      if (response.status === 401 || response.status === 403) {
        throw Object.assign(new Error('登录已失效，请重新登录。'), {retryable:false, authExpired:true});
      }
      if (!response.ok) throw new Error('历史读取失败');
      return response.json();
    };
    const checkIdentity = async () => {
      const identity = await read('/api/me');
      if (!identity.user || identity.user.id !== userId) {
        throw Object.assign(new Error('登录已失效或账号已改变，请重新登录。'), {retryable:false, authExpired:true});
      }
    };
    await checkIdentity();
    const messages = [];
    let total;
    do {
      const data = await read(`/api/conversation?offset=${messages.length}&limit=100`);
      if (!Array.isArray(data.messages) || !Number.isSafeInteger(data.total) || data.total < 0) {
        throw new Error('历史格式错误');
      }
      if (total !== undefined && total !== data.total) throw new Error('历史数量发生变化，请重新同步。');
      total = data.total;
      if (data.messages.length !== Math.min(100, total - messages.length)) throw new Error('历史分页不完整');
      messages.push(...data.messages);
      onProgress(messages.length, total);
    } while (messages.length < total);
    await checkIdentity();
    return messages;
  }

  class ChatRecovery {
    constructor({fetchHistory, onMessages, onStatus, onBusy,
      sleep = ms => new Promise(resolve => setTimeout(resolve, ms)),
      isOnline = () => navigator.onLine, isActive = () => true, onAuthExpired = () => {}}) {
      Object.assign(this, {fetchHistory, onMessages, onStatus, onBusy, sleep, isOnline, isActive, onAuthExpired});
      this.sending = false;
      this.syncing = false;
      this.pendingSync = false;
      this.uncertain = false;
      this.syncController = null;
    }

    beginSend() {
      if (!this.isActive()) return false;
      if (this.sending || this.syncing) return false;
      if (!this.isOnline()) {
        this.onStatus('网络已断开，输入内容已保留，联网后再发送。');
        return false;
      }
      this.sending = true;
      this.onBusy(true);
      return true;
    }

    async finishSend(uncertain) {
      this.sending = false;
      this.uncertain = this.uncertain || uncertain;
      this.onBusy(false);
      if (uncertain) this.onStatus('发送结果不确定，服务可能仍在处理。请同步历史，勿重复提交。');
      if (this.pendingSync) {
        this.pendingSync = false;
        await this.sync();
      }
    }

    cancelSync() {
      if (!this.syncing) return false;
      const controller = this.syncController;
      this.syncController = null;
      this.syncing = false;
      controller.abort();
      this.onBusy(false);
      this.onStatus('已取消历史同步，原展示和草稿已保留。');
      return true;
    }

    async sync() {
      if (!this.isActive()) return;
      if (this.sending) { this.pendingSync = true; return; }
      if (this.syncing) return;
      if (!this.isOnline()) { this.onStatus('网络已断开，联网后将重新同步历史。'); return; }
      this.syncing = true;
      const controller = this.syncController = new AbortController();
      const current = () => this.syncController === controller;
      const cancelled = new Promise(resolve => controller.signal.addEventListener('abort', () => resolve(null), {once:true}));
      this.onBusy(true);
      try {
        for (let attempt = 0; attempt < 3; attempt++) {
          if (!current()) return;
          if (!this.isOnline()) { this.onStatus('网络已断开，联网后将重新同步历史。'); return; }
          this.onStatus(attempt ? `正在重试同步历史（${attempt + 1}/3）…` : '正在同步历史…');
          try {
            const messages = await Promise.race([this.fetchHistory((loaded, total) => {
              if (!current()) return;
              const action = attempt ? `正在重试同步历史（${attempt + 1}/3）` : '正在同步历史';
              this.onStatus(`${action}：已读取 ${loaded}/${total} 条${loaded === total ? '，正在核对身份…' : '…'}`);
            }, controller.signal), cancelled]);
            if (!current()) return;
            this.onMessages(messages);
            this.onStatus(this.uncertain
              ? '已同步已保存消息；服务可能仍在处理，请稍后再同步，勿重复提交。'
              : '历史已同步。');
            return;
          } catch (error) {
            if (!current()) return;
            if (error.authExpired) this.onAuthExpired(error.message);
            if (error.retryable === false) { this.onStatus(error.message); return; }
            if (attempt === 2) { this.onStatus('历史同步失败，请检查网络后点击「同步历史」。'); return; }
            await Promise.race([this.sleep(1000 * (attempt + 1)), cancelled]);
          }
        }
      } finally {
        if (current()) {
          this.syncController = null;
          this.syncing = false;
          this.onBusy(false);
        }
      }
    }
  }
  if (typeof module !== 'undefined' && module.exports) module.exports = {ChatRecovery, fetchPagedHistory};
  else { root.ChatRecovery = ChatRecovery; root.fetchPagedHistory = fetchPagedHistory; }
})(globalThis);
