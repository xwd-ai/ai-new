/* HTTP 历史恢复只重试读取，绝不自动重发对话 POST。 */
(function (root) {
  'use strict';
  class ChatRecovery {
    constructor({fetchHistory, onMessages, onStatus, onBusy,
      sleep = ms => new Promise(resolve => setTimeout(resolve, ms)),
      isOnline = () => navigator.onLine}) {
      Object.assign(this, {fetchHistory, onMessages, onStatus, onBusy, sleep, isOnline});
      this.sending = false;
      this.syncing = false;
      this.pendingSync = false;
      this.uncertain = false;
    }

    beginSend() {
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

    async sync() {
      if (this.sending) { this.pendingSync = true; return; }
      if (this.syncing) return;
      if (!this.isOnline()) { this.onStatus('网络已断开，联网后将重新同步历史。'); return; }
      this.syncing = true;
      this.onBusy(true);
      try {
        for (let attempt = 0; attempt < 3; attempt++) {
          if (!this.isOnline()) { this.onStatus('网络已断开，联网后将重新同步历史。'); return; }
          this.onStatus(attempt ? `正在重试同步历史（${attempt + 1}/3）…` : '正在同步历史…');
          try {
            const messages = await this.fetchHistory();
            this.onMessages(messages);
            this.onStatus(this.uncertain
              ? '已同步已保存消息；服务可能仍在处理，请稍后再同步，勿重复提交。'
              : '历史已同步。');
            return;
          } catch (error) {
            if (error.retryable === false) { this.onStatus(error.message); return; }
            if (attempt === 2) { this.onStatus('历史同步失败，请检查网络后点击「同步历史」。'); return; }
            await this.sleep(1000 * (attempt + 1));
          }
        }
      } finally {
        this.syncing = false;
        this.onBusy(false);
      }
    }
  }
  if (typeof module !== 'undefined' && module.exports) module.exports = {ChatRecovery};
  else root.ChatRecovery = ChatRecovery;
})(globalThis);
