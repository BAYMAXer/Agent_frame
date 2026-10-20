/* Manual Jev requests are separate from agent runs and SFT collection. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const presets = {'vllm-sr/d3-flash':'http://10.44.101.62:8007/v1/systemone'};
  $('judge-model').addEventListener('change', () => {
    const field = $('judge-url');
    if (!field.value.trim() || Object.values(presets).includes(field.value.trim())) field.value = presets[$('judge-model').value] || '';
  });
  $('jev-model').addEventListener('change', () => { $('jev-url').value = presets[$('jev-model').value]; });
  const criteria = {
    choice:{passed:'任务已完成',failed:'任务未完成或失败',pending:'操作仍在进行中',unknown:'证据不足无法判断'},
    score:['未开始','进行中','基本完成','完全完成']
  };
  function changeType() {
    const kind = $('jev-type').value;
    $('jev-criteria-field').hidden = kind === 'noul';
    $('jev-criteria').value = JSON.stringify(criteria[kind] || {}, null, 2);
    $('jev-question').value = kind === 'noul' ? '根据截图和补充证据，任务是否已经完成？' : kind === 'score' ? '根据截图和补充证据，任务完成度属于哪个等级？' : '根据截图和补充证据，任务完成状态属于哪一种？证据不足时选择 unknown。';
  }
  $('jev-type').addEventListener('change', changeType); changeType();
  let files = [], busy = false;
  const urls = [];
  function error(message) { $('jev-error').textContent = message; $('jev-error').hidden = !message; }
  function clearPreviews() { urls.splice(0).forEach(url => URL.revokeObjectURL(url)); $('jev-previews').replaceChildren(); }
  function setFiles(next) {
    if (next.length > 4 || next.some(f => f.size > 9 * 1024 * 1024 || !['image/png','image/jpeg','image/webp'].includes(f.type))) {
      error('请选择最多 4 张 PNG / JPEG / WebP 截图，每张不超过 9 MiB。');
      $('jev-images').value = ''; return;
    }
    error(''); clearPreviews(); files = next;
    files.forEach((file, i) => {
      const figure = document.createElement('figure'), img = document.createElement('img'), caption = document.createElement('figcaption');
      const url = URL.createObjectURL(file); urls.push(url); img.src = url; img.alt = `截图 ${i+1}`;
      caption.textContent = `${i+1}. ${file.name}`; figure.append(img, caption); $('jev-previews').append(figure);
    });
    $('jev-clear').hidden = !files.length;
  }
  $('jev-images').addEventListener('change', event => setFiles(Array.from(event.target.files)));
  $('jev-clear').addEventListener('click', () => { setFiles([]); $('jev-images').value = ''; });
  function readFile(file) { return new Promise((resolve,reject) => {
    const reader = new FileReader(); reader.onload = () => resolve(reader.result); reader.onerror = () => reject(new Error('截图读取失败')); reader.readAsDataURL(file);
  }); }
  function config() {
    const timeout = Number($('jev-timeout').value);
    if (!Number.isFinite(timeout) || timeout < 1 || timeout > 600) throw new Error('超时需要在 1–600 秒之间');
    if (!$('jev-url').value.trim()) throw new Error('请填写 Jev 服务地址');
    return {model:$('jev-model').value,url:$('jev-url').value.trim(),api_key:$('jev-key').value,timeout};
  }
  function questionPayload() {
    if ($('jev-questions').value.trim()) return JSON.parse($('jev-questions').value);
    const id = $('jev-question-id').value.trim(); if (!id) throw new Error('问题 ID 不能为空');
    const question = {type:$('jev-type').value,instructions:$('jev-question').value};
    if (question.type !== 'noul') question.criteria = JSON.parse($('jev-criteria').value);
    return {[id]:question};
  }
  function para(text, target) { const p = document.createElement('p'); p.textContent = text; target.append(p); }
  function render(result) {
    const container = $('jev-results'); container.replaceChildren();
    const raw = result.raw_response || result;
    $('jev-raw').textContent = JSON.stringify(raw, null, 2); $('jev-raw-panel').hidden = false;
    if (result.health || result.models) { para(result.ok ? '服务与模型能力查询成功。详情见原始响应。' : '部分端点不可用，详情见原始响应。', container); return; }
    para(`实际模型：${result.model || '未返回'} · 输入 tokens：${result.usage?.input_tokens ?? '—'} · 输出 tokens：${result.usage?.output_tokens ?? '—'}`, container);
    for (const [id, answer] of Object.entries(raw.answers || {})) {
      const card = document.createElement('div'); card.className = 'jev-answer';
      const title = document.createElement('h3'); title.textContent = `${id} · ${answer.type}`; card.append(title);
      if (answer.type === 'noul') para(`“是”的模型分数：${(answer.noul * 100).toFixed(2)}%`, card);
      if (answer.type === 'choice') {
        para(`最高选项：${answer.choice}`, card);
        const normalized = result.answers?.[id];
        if (normalized) para(`框架采信结果：${normalized.label} · 阈值 0.90 / 分差 0.15（调试默认）`, card);
      }
      if (answer.type === 'score') para(`有序等级加权得分：${Number(answer.score).toFixed(3)}`, card);
      for (const [label, probability] of Object.entries(answer.probabilities || {})) {
        const row = document.createElement('div'); row.className = 'jev-prob';
        const name = document.createElement('span'), bar = document.createElement('progress'), value = document.createElement('span');
        name.textContent = answer.legend?.[label] ? `${label} · ${answer.legend[label]}` : label;
        bar.max = 1; bar.value = probability; bar.setAttribute('aria-label', name.textContent);
        value.textContent = `${(probability*100).toFixed(1)}%`; row.append(name,bar,value); card.append(row);
      }
      if (answer.confidence != null) para(`分布集中度（1 − 归一化熵）：${Number(answer.confidence).toFixed(3)}`, card);
      container.append(card);
    }
  }
  async function send(probe) {
    if (busy) return;
    error('');
    let payload;
    try {
      payload = {config:config()};
      if (!probe) Object.assign(payload, {state:$('jev-state').value, questions:questionPayload()});
    } catch (e) { error(e.message); return; }
    busy = true; $('jev-send').disabled = $('jev-probe').disabled = $('jev-demo').disabled = true;
    $('jev-images').disabled = $('jev-clear').disabled = true;
    const output = document.querySelector('.jev-output'); output.classList.add('busy'); $('jev-status').textContent = 'RUNNING';
    $('jev-results').replaceChildren(); $('jev-raw-panel').hidden = true;
    const started = performance.now();
    const tick = () => { $('jev-timing').textContent = `请求中 · ${((performance.now()-started)/1000).toFixed(1)} s（含网络与排队）`; }; tick();
    const timer = setInterval(tick,100);
    try {
      if (!probe) payload.images = await Promise.all(files.map(readFile));
      const response = await fetch(probe ? '/jev/probe' : '/jev/decide', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
      const result = await response.json();
      if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : JSON.stringify(result.detail || result));
      render(result); $('jev-status').textContent = result.ok ? 'DONE' : 'UNKNOWN';
      $('jev-timing').textContent = `服务请求耗时 ${Number(result.latency_ms || 0).toFixed(0)} ms · 页面总耗时 ${((performance.now()-started)/1000).toFixed(2)} s`;
      if (!result.ok) error([result.reason || '服务检查未通过', result.http_status ? `HTTP ${result.http_status}` : '', result.detail ? JSON.stringify(result.detail) : result.error_type || ''].filter(Boolean).join(' · '));
    } catch (e) {
      error(e.message); $('jev-status').textContent = 'ERROR'; $('jev-timing').textContent = `请求结束 · ${((performance.now()-started)/1000).toFixed(2)} s`;
    } finally {
      clearInterval(timer); busy = false; output.classList.remove('busy');
      $('jev-send').disabled = $('jev-probe').disabled = $('jev-demo').disabled = $('jev-images').disabled = $('jev-clear').disabled = false;
    }
  }
  $('jev-send').addEventListener('click', () => send(false));
  $('jev-probe').addEventListener('click', () => send(true));
  $('jev-demo').addEventListener('click', () => {
    if (busy) return;
    setFiles([]); $('jev-images').value = '';
    $('jev-type').value = 'choice'; changeType();
    $('jev-question-id').value = 'completion'; $('jev-questions').value = '';
    $('jev-state').value = '演示样例（模拟证据）：用户要求打开 example.org 首页。当前浏览器 URL 为 https://example.org/，页面标题显示 Example Domain，页面主体包含 Example Domain。没有其它任务要求。';
    $('jev-question').value = '根据给出的任务和可观察证据，任务目前处于什么状态？';
    send(false);
  });
})();
