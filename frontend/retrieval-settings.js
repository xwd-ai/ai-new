'use strict';

const byId = id => document.getElementById(id);
const names = ['prefetch_floor', 'prefetch_multiplier', 'rrf_k', 'vector_weight', 'keyword_weight', 'mmr_lambda'];
const token = localStorage.getItem('token') || '';
let saved = null;
let busy = false;
let authorized = false;

function status(id, message, error = false) {
  byId(id).textContent = message;
  byId(id).classList.toggle('error', error);
}

function values() {
  return Object.fromEntries(names.map(name => [name, Number(byId(name).value)]));
}

function dirty() {
  return saved && names.some(name => values()[name] !== saved[name]);
}

function controls() {
  for (const id of ['settingsFields', 'fusionFields', 'mmrFields', 'previewFields']) {
    byId(id).disabled = !authorized || busy || !saved;
  }
  byId('save').disabled = !authorized || busy || !saved || !dirty();
  byId('undo').disabled = byId('save').disabled;
  byId('preview').disabled = !authorized || busy || !saved || dirty();
}

function fill(data) {
  for (const name of names) byId(name).value = data[name];
  byId('mmrValue').value = data.mmr_lambda;
  controls();
}

async function request(path, options = {}) {
  const response = await fetch(path, {
    ...options, headers: {'Authorization': `Bearer ${token}`, 'Content-Type': 'application/json'},
  });
  if (response.status === 403) {
    authorized = false;
    byId('loginLink').hidden = false;
    status('access', '需要有效的管理员账号，请重新登录。', true);
    controls();
    throw new Error('没有管理员权限，设置未保存。');
  }
  if (!response.ok) {
    if (response.status === 422) throw new Error('参数不合法，请检查范围、整数和两路权重。');
    if (response.status === 503) throw new Error('保存失败，请检查配置目录写权限或稍后重试。');
    throw new Error('服务暂时不可用，请稍后重试。');
  }
  return response.json();
}

byId('settingsForm').addEventListener('input', () => {
  byId('mmrValue').value = byId('mmr_lambda').value;
  byId('result').hidden = true;
  status('saveStatus', dirty() ? '有未保存修改，保存后才能试查。' : '与已加载设置一致。');
  status('previewStatus', '');
  controls();
});

byId('undo').addEventListener('click', () => {
  fill(saved);
  status('saveStatus', '已撤销未保存修改。');
});

byId('settingsForm').addEventListener('submit', async event => {
  event.preventDefault();
  if (!authorized || busy || !saved) return;
  const data = values();
  if (data.vector_weight + data.keyword_weight <= 0) {
    status('saveStatus', '两路召回权重不能同时为 0。', true);
    return;
  }
  busy = true;
  controls();
  status('saveStatus', '正在保存…');
  byId('result').hidden = true;
  try {
    saved = await request('/api/knowledge/settings', {method: 'PUT', body: JSON.stringify(data)});
    fill(saved);
    status('saveStatus', '设置已保存，后续检索调用将读取新值。');
  } catch (error) {
    status('saveStatus', error instanceof TypeError ? '网络连接失败，请检查连接后重试。' : error.message, true);
  } finally {
    busy = false;
    controls();
  }
});

const stageNames = {keyword_recall: '关键词召回', vector_recall: '向量召回', rrf_fusion: '两路融合', rerank: '相关性重排', mmr: '多样性筛选'};
byId('previewForm').addEventListener('submit', async event => {
  event.preventDefault();
  if (!authorized || busy || !saved || dirty()) return;
  const query = byId('query').value.trim();
  if (!query) {
    status('previewStatus', '请输入要查找的内容。', true);
    return;
  }
  busy = true;
  controls();
  byId('result').hidden = true;
  status('previewStatus', '正在检索…');
  try {
    const params = new URLSearchParams({q: query, top_k: byId('topK').value});
    const data = await request(`/api/knowledge/diagnose?${params}`);
    byId('stages').replaceChildren();
    for (const stage of data.stages) {
      const row = document.createElement('tr');
      const skipped = stage.skipped || (stage.detail && stage.detail.skipped);
      const state = skipped ? (stage.stage === 'rrf_fusion' ? '使用关键词结果' : '已跳过') : '已执行';
      for (const [index, text] of [stageNames[stage.stage] || stage.stage, stage.count, state].entries()) {
        const cell = document.createElement('td');
        cell.textContent = text;
        if (index === 1) cell.className = 'count';
        row.append(cell);
      }
      byId('stages').append(row);
    }
    byId('depth').textContent = `候选上限 ${data.prefetch_depth} 条 · 目标 ${data.top_k} 条`;
    byId('finalIds').textContent = data.final_ids.length ? data.final_ids.join('、') : '没有命中来源，请换一个问题或检查资料。';
    byId('result').hidden = false;
    status('previewStatus', '试查完成。其他管理员可能同时更新服务设置，本次以服务读取值为准。');
  } catch (error) {
    status('previewStatus', error instanceof TypeError ? '网络连接失败，请检查连接后重试。' : error.message, true);
  } finally {
    busy = false;
    controls();
  }
});

async function initialize() {
  try {
    const identity = await request('/api/me');
    if (!identity.user || identity.user.role !== 'admin') {
      byId('loginLink').hidden = false;
      status('access', '仅管理员可以调整检索设置，请先登录管理员账号。', true);
      return;
    }
    authorized = true;
    saved = await request('/api/knowledge/settings');
    fill(saved);
    status('access', `管理员：${identity.user.name || identity.user.username}`);
    status('saveStatus', '已加载当前生效设置。');
  } catch (error) {
    status('access', '无法加载检索设置，请检查连接或登录状态后刷新页面。', true);
  } finally {
    controls();
  }
}
initialize();
