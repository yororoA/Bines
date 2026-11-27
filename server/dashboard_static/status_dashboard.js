(() => {
  'use strict';
  const $ = (id) => document.getElementById(id);
  const esc = (value) => String(value ?? '').replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const number = (value) => typeof value === 'number' && Number.isFinite(value) ? value.toLocaleString('zh-CN') : '—';
  const time = (value) => typeof value === 'number' && value > 0 ? new Date(value * 1000).toLocaleString('zh-CN', {hour12:false}) : '暂无记录';
  const empty = (message) => `<div class="empty">${esc(message)}</div>`;
  const statusLabel = {online:'在线', offline:'离线', unknown:'未知'};
  const jobLabel = {pending:'等待', running:'处理中', done:'完成', failed:'失败'};
  const serviceLabel = {ready:'已就绪', stopped:'已停止', failed:'失败', blocked:'依赖不可用', starting:'启动中', stopping:'停止中'};
  const keyLabel = {mood:'心情',emotion:'情绪',relationship_score:'关系分值',current_focus:'当前关注',activity:'当前活动',summary:'状态摘要',last_updated:'最近更新',desire:'意愿',energy:'精力',thoughts:'想法',goal:'目标'};
  const ports = JSON.parse($('dashboard-config').textContent);
  function links() {
    document.querySelectorAll('[data-port]').forEach((anchor) => {
      const port = ports[anchor.dataset.port];
      if (!Number.isInteger(port) || port < 1 || port > 65535) return;
      const url = new URL(window.location.href);
      url.port = String(port); url.pathname = anchor.dataset.path || '/'; url.search = ''; url.hash = '';
      anchor.href = url.toString();
    });
  }
  links();
  function facts(rows) {
    return `<div class="facts">${rows.map(([value,label]) => `<div class="fact"><strong>${number(value)}</strong><span>${esc(label)}</span></div>`).join('')}</div>`;
  }
  function sum(values) { return values.reduce((total,value) => total + (typeof value === 'number' ? value : 0), 0); }
  function renderHealth(components) {
    const definitions = [['memory','记忆服务'],['gateway','QQ 网关'],['autonomy','自主状态存储'],['services','本机进程管理']];
    $('health-cards').innerHTML = definitions.map(([key,label]) => {
      const component = components[key] || {status:'unknown'};
      let detail = component.error || (component.status === 'online' ? '本次探测成功' : '尚未取得状态');
      if (key === 'gateway' && component.data) detail = component.data.connected === true ? 'NapCat / QQ 已连接' : component.data.connected === false ? '网关在线 · QQ 未连接' : 'QQ 连接情况未知';
      if (key === 'autonomy' && component.data) detail = component.data.configured === false ? '尚无自主状态数据' : `${component.data.backend} · ${{pause:'自主活动已暂停',stop:'自主活动已停止',resume:'允许自主活动',auto:'自动模式'}[component.data.control] || '控制状态未知'}`;
      if (key === 'services' && component.data?.managed === false) detail = '此入口不管理本机进程';
      return `<article class="health-card"><div class="label">${esc(label)}<span class="dot ${esc(component.status)}"></span></div><strong>${esc(statusLabel[component.status] || '未知')}</strong><small>${esc(detail)}</small></article>`;
    }).join('');
  }
  function renderLife(memory) {
    const life = memory?.life;
    $('life-phase').textContent = life?.phase_label || '未知';
    if (!life) { $('life-content').innerHTML = empty('生命状态暂不可用'); return; }
    const meter = (name,value,kind='') => {
      const percent = typeof value === 'number' && Number.isFinite(value) ? Math.round(Math.min(1,Math.max(0,value))*100) : null;
      return `<div class="meter ${kind}"><label><span>${name}</span><span>${percent === null ? '未知' : percent+'%'}</span></label><div class="meter-track"><i style="width:${percent || 0}%"></i></div></div>`;
    };
    $('life-content').innerHTML = `<div class="life-age">第 ${number(life.day_number ?? life.age_days)} 天<small>与世界相处</small></div>${meter('精力',life.energy)}${meter('睡眠压力',life.sleep_pressure,'sleep')}<p class="detail">当前关注：${esc(life.current_focus || '暂无')}</p><p class="detail">最近互动：${esc(time(life.last_human_at))}</p>`;
  }
  function renderState(memory) {
    if (!memory || memory.state_unavailable || !memory.state) { $('state-content').innerHTML = empty('动态状态暂不可用'); return; }
    const state = memory.state;
    const rows = Object.entries(state.data || {}).filter(([key]) => !key.startsWith('_'));
    $('state-content').innerHTML = rows.length ? `<dl class="state-list">${rows.map(([key,value]) => `<div class="state-row"><dt>${esc(keyLabel[key] || key)}</dt><dd>${esc(typeof value === 'object' ? JSON.stringify(value,null,2) : value)}</dd></div>`).join('')}</dl><p class="detail">版本 ${number(state.version)} · ${esc(time(state.updated_at))}</p>` : empty('尚未记录动态状态');
  }
  function renderMemory(memory) {
    if (!memory) { $('memory-content').innerHTML = empty('记忆服务暂不可用；原有记忆不会因此消失。'); return; }
    const total = memory.memories ? sum(Object.values(memory.memories).flatMap((kinds) => Object.values(kinds))) : null;
    const messages = memory.messages ? sum(Object.values(memory.messages)) : null;
    const vector = memory.vector_status || {};
    const indexState = vector.degraded ? '部分索引已隔离，全文检索仍可用' : vector.building ? '正在重建索引' : vector.ready === true ? '向量检索已就绪' : vector.ready === false ? '向量尚未就绪' : '索引类型未报告';
    $('memory-content').innerHTML = facts([[total,'长期记忆'],[messages,'原始消息'],[memory.index_count,'可检索索引'],[memory.association_nodes,'关联节点'],[memory.association_edges,'记忆关联'],[memory.unindexed,'待索引']]) + `<p class="detail">${esc(indexState)}</p>`;
  }
  function queueCounts(counts) {
    return `<div class="queue-line">${Object.entries(jobLabel).map(([key,label]) => `<span class="${key === 'failed' && counts?.[key] > 0 ? 'warning' : ''}">${label} ${number(counts?.[key] ?? 0)}</span>`).join('')}</div>`;
  }
  function renderQueues(memory,gateway) {
    const queue = gateway?.queue;
    const memoryPart = memory?.jobs ? `<span class="muted">记忆维护任务</span>${queueCounts(memory.jobs)}` : empty('记忆任务状态暂不可用');
    const qqPart = queue ? `<span class="muted">QQ 消息处理</span>${facts([[queue.workers_live,'在线工作线程'],[queue.pending_due,'到期待处理'],[queue.dead_letters,'失败消息']])}<p class="detail">处理中 ${number(queue.workers_processing)} · 等待记忆恢复 ${number(queue.recovery_pending)} · 延迟重试 ${number(queue.pending_delayed)}</p>` : empty('QQ 消息队列暂不可用');
    $('queue-content').innerHTML = memoryPart + qqPart;
  }
  function renderUsage(memory) {
    const rows = memory?.model_usage_24h;
    if (!Array.isArray(rows)) { $('usage-content').innerHTML = empty('用量统计暂不可用'); return; }
    if (!rows.length) { $('usage-content').innerHTML = empty('最近 24 小时暂无模型调用记录'); return; }
    const kind = {qq:'QQ 对话',desktop:'桌面对话',autonomous:'自主活动',other:'其他'};
    $('usage-content').innerHTML = `<div class="table-wrap"><table><thead><tr><th>任务 / 模型</th><th>调用</th><th>输入 token</th><th>输出 token</th><th>缓存命中率</th><th>错误 / 截断</th><th>用量缺失</th></tr></thead><tbody>${rows.map((row) => `<tr><td>${esc(kind[row.kind] || row.kind)}<br><span class="muted">${esc(row.model)}</span></td><td>${number(row.calls)}</td><td>${number(row.prompt_tokens)}</td><td>${number(row.completion_tokens)}</td><td>${typeof row.cache_hit_ratio === 'number' ? number(Math.round(row.cache_hit_ratio*100))+'%' : '未知'}</td><td>${number(row.errors)} / ${number(row.truncated)}</td><td>${number(row.unknown_usage_calls)}</td></tr>`).join('')}</tbody></table></div><p class="detail">缺少 usage 的调用不计为 0 token；表中 token 为已记录部分，不据此推算费用。</p>`;
  }
  function renderServices(component) {
    if (!component?.data) { $('services-content').innerHTML = empty('进程状态暂不可用'); return; }
    if (!component.data.managed) { $('services-content').innerHTML = empty('当前记忆 Web 入口没有本机进程管理器。请通过“模块管理”查看宿主进程；容器进程由 Docker 管理。上方服务状态仍显示实际连接结果。'); return; }
    const rows = component.data.items || [];
    $('services-content').innerHTML = rows.length ? `<div class="service-grid">${rows.map((row) => `<div class="service"><b>${esc(row.name)}</b><small><span class="dot ${row.running && row.state === 'ready' ? 'online' : row.state === 'failed' ? 'offline' : ''}"></span>${esc(serviceLabel[row.state] || row.state || '未知')}${row.pid ? ' · PID '+esc(row.pid) : ''}</small></div>`).join('')}</div>` : empty('此管理器没有本机服务，可能由容器统一管理。');
  }
  let loading = false;
  async function refresh() {
    if (loading) return;
    loading = true; $('refresh').disabled = true;
    try {
      const response = await fetch('/api/status/overview', {signal:AbortSignal.timeout(20000)});
      const data = await response.json();
      if (!response.ok || !data.success) throw new Error('无法读取状态，请重新打开本地管理页面后重试。');
      const components = data.result.components;
      renderHealth(components); renderLife(components.memory.data); renderState(components.memory.data);
      renderMemory(components.memory.data); renderQueues(components.memory.data,components.gateway.data);
      renderUsage(components.memory.data); renderServices(components.services);
      $('updated-at').textContent = `更新于 ${time(data.result.updated_at)}`;
      $('page-error').hidden = true;
    } catch (error) {
      $('page-error').textContent = '刷新失败，当前显示可能已过期。请检查管理服务是否在线，或重新打开页面。';
      $('page-error').hidden = false;
      if (!$('health-cards').children.length) {
        renderHealth({}); renderLife(null); renderState(null); renderMemory(null);
        renderQueues(null,null); renderUsage(null); renderServices(null);
      }
    } finally { loading = false; $('refresh').disabled = false; }
  }
  $('refresh').addEventListener('click',refresh);
  setInterval(() => { if ($('auto-refresh').checked && !document.hidden) refresh(); },15000);
  document.addEventListener('visibilitychange',() => { if (!document.hidden && $('auto-refresh').checked) refresh(); });
  async function errorMessage(response) {
    try { const data = await response.json(); return data.error || '请求未成功，请稍后重试。'; }
    catch { return '请求未成功，请检查管理服务。'; }
  }
  $('download-bundle').addEventListener('click',async () => {
    const button = $('download-bundle'); button.disabled = true;
    $('bundle-message').textContent = '正在创建一致性快照，完成后将下载…';
    try {
      const response = await fetch('/api/memory/bundle');
      if (!response.ok) throw new Error(await errorMessage(response));
      const blob = await response.blob();
      const url = URL.createObjectURL(blob), anchor = document.createElement('a');
      const name = response.headers.get('Content-Disposition')?.match(/filename="?([^";]+)"?/i)?.[1];
      anchor.href = url; anchor.download = name?.replace(/[\\/]/g,'_') || 'bines-memory.zip';
      document.body.append(anchor); anchor.click(); anchor.remove();
      setTimeout(() => URL.revokeObjectURL(url),60000);
      $('bundle-message').textContent = '迁移包已生成并开始下载。请在目标设备导入前校验文件。';
    } catch (error) { $('bundle-message').textContent = `下载失败：${error.message}`; }
    finally { button.disabled = false; }
  });
  $('inspect-bundle').addEventListener('click',async () => {
    const file = $('bundle-file').files[0];
    if (!file) { $('bundle-message').textContent = '请先选择一个迁移包。'; return; }
    if (file.size > 512 * 1024 * 1024) { $('bundle-message').textContent = '文件超过 512 MiB，请使用本机 inspect-bundle 命令校验。'; return; }
    const button = $('inspect-bundle'); button.disabled = true;
    $('bundle-inspection').hidden = true; $('bundle-message').textContent = '正在校验迁移包…';
    try {
      const response = await fetch('/api/memory/bundle/inspect',{method:'POST',headers:{'Content-Type':'application/zip'},body:file});
      if (!response.ok) throw new Error(await errorMessage(response));
      const data = await response.json();
      if (!data.success) throw new Error(data.error || '校验未通过');
      $('bundle-message').textContent = '校验通过。文件未导入，当前记忆未改变。';
      $('bundle-inspection').textContent = JSON.stringify(data.result,null,2);
      $('bundle-inspection').hidden = false;
    } catch (error) { $('bundle-message').textContent = `校验失败：${error.message}`; }
    finally { button.disabled = false; }
  });
  refresh();
})();
