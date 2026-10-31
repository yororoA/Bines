/* Reuses the existing memory editor, API client and navigation. */
const SOURCE_LABEL = { ...KIND_LABEL, raw: "原始回合" };
const RELATION_LABEL = { support: "相互支持", similar: "相似经历", event: "同一事件", cause: "因果", contradicts: "存在矛盾" };
const JOB_LABEL = { associate: "记忆建联", summarize: "对话摘要", diary: "日记巩固", update_state: "状态更新", summarize_experience: "经历摘要", repair_memory: "纠错重建", audit_memory: "自动纠错审查" };
const STATUS_LABEL = { pending: "等待中", running: "处理中", done: "已完成", failed: "失败" };
let nodeOffset = 0, nodeTotal = 0, detailMemoryId = null, nodeRequest = 0, detailRequest = 0;
let noticeTimer;
let currentDetail = null, correctionRequest = null;
const empty = (text) => `<div class="empty">${esc(text)}</div>`;
const jsonDisclosure = (label, value) => `<details><summary>${esc(label)}</summary><pre>${esc(JSON.stringify(value, null, 2))}</pre></details>`;
const badge = (status) => `<span class="tag badge-${esc(status)}">${esc(status === "hot" ? "活跃记忆" : status === "cold" ? "冷层记忆" : STATUS_LABEL[status] || status)}</span>`;

function notify(message) {
  clearTimeout(noticeTimer);
  $("notice").textContent = message;
  $("notice").classList.remove("hidden");
  noticeTimer = setTimeout(() => $("notice").classList.add("hidden"), 6000);
}

function renderOverview(stats) {
  $("service-status").textContent = stats.retention_enabled ? "记忆网络运行中" : "已连接 · 留存未启用";
  const metrics = [
    [stats.index_count || 0, "长期记忆 / 向量索引"],
    [stats.association_nodes || 0, "原始回合与记忆节点"],
    [stats.association_edges || 0, "有来源依据的关联"],
    [stats.association_ready || 0, "已完成建联判断"],
    [stats.nodes_by_status?.hot || 0, "活跃记忆"],
    [stats.nodes_by_status?.cold || 0, "冷层记忆"],
    [stats.memory_usage_events || 0, "呈现、引用与复用记录"],
    [stats.consolidation_snapshots || 0, "巩固快照 · 原文 Top3 / 摘要 Top1"],
  ];
  $("overview-cards").innerHTML = metrics.map(([value, label]) => `<div class="metric"><strong>${value.toLocaleString()}</strong><span>${label}</span></div>`).join("");
  const selected = $("n-scope").value;
  $("n-scope").innerHTML = $("f-scope").innerHTML;
  $("n-scope").value = selected;
}

async function loadNodes() {
  const serial = ++nodeRequest;
  const params = new URLSearchParams({ offset: nodeOffset, limit: PAGE, source_type: $("n-type").value,
    status: $("n-status").value, ready: $("n-ready").value, keyword: $("n-keyword").value.trim() });
  if ($("n-scope").value) params.set("scopes", $("n-scope").value);
  $("node-list").classList.add("loading");
  try {
    const data = await api("/api/nodes?" + params);
    if (serial !== nodeRequest) return;
    nodeTotal = data.total;
    $("node-summary").textContent = `共 ${nodeTotal} 个节点 · 历史数据正在逐批建联`;
    $("node-page").textContent = `${nodeTotal ? nodeOffset + 1 : 0}–${nodeOffset + data.items.length} / ${nodeTotal}`;
    $("n-prev").disabled = nodeOffset === 0;
    $("n-next").disabled = nodeOffset + PAGE >= nodeTotal;
    $("node-list").innerHTML = data.items.map((n) => `<button class="node-card" data-node="${esc(n.id)}">
      <div class="row"><span class="tag kind-${esc(n.source_type)}">${esc(SOURCE_LABEL[n.source_type] || n.source_type)}</span>${badge(n.retrieval_status)}${!n.ready ? '<span class="tag badge-pending">等待建联</span>' : ""}</div>
      <div class="content">${esc(n.content)}</div>
      <div class="node-footer"><span>${esc(scopeLabel(n.scope))} · ${esc(n.day_key || "")}</span><span>${n.chain_count} 个关联理由 ↗</span></div>
    </button>`).join("") || empty("当前筛选下没有记忆节点");
  } finally {
    if (serial === nodeRequest) $("node-list").classList.remove("loading");
  }
}

function weightBar(label, value, citation = false) {
  const percent = Math.round(Math.max(0, Math.min(1, Number(value) || 0)) * 1000) / 10;
  return `<div><div class="weight-label"><span>${label}</span><span>${percent}%</span></div><div class="weight-track ${citation ? "citation" : ""}"><i style="width:${percent}%"></i></div></div>`;
}

function neighborhood(detail) {
  const neighbors = [...new Map(detail.edges.map((e) => [e.other.id, e.other])).values()].slice(0, 6);
  if (!neighbors.length) return "";
  const cx = 350, cy = 106;
  let lines = "", nodes = "";
  neighbors.forEach((n, i) => {
    const angle = 2 * Math.PI * i / neighbors.length;
    const x = cx + Math.cos(angle) * 245, y = cy + Math.sin(angle) * 78;
    lines += `<line x1="${cx}" y1="${cy}" x2="${x}" y2="${y}" stroke="#b6c8ad" stroke-width="1.4"/>`;
    nodes += `<circle cx="${x}" cy="${y}" r="17" fill="${n.retrieval_status === "hot" ? "#d7e6cd" : "#dce4e7"}"/><text x="${x}" y="${y + 32}" text-anchor="middle" fill="#607361" font-size="11">${esc(SOURCE_LABEL[n.source_type] || n.source_type)}</text>`;
  });
  return `<div class="neighborhood"><svg viewBox="0 0 700 225" role="img" aria-label="当前记忆与 ${neighbors.length} 个相邻记忆的关系示意">${lines}${nodes}<circle cx="${cx}" cy="${cy}" r="27" fill="#35674d"/><text x="${cx}" y="${cy + 4}" text-anchor="middle" fill="#fff" font-size="11">当前记忆</text></svg><p class="muted">下方可打开相邻记忆，沿原文依据查看关联。</p></div>`;
}

async function openMemoryDetail(params) {
  const serial = ++detailRequest;
  const dialog = $("memory-detail");
  $("detail-body").innerHTML = empty("正在读取记忆详情…");
  currentDetail = null;
  $("detail-edit").classList.add("hidden");
  if (!dialog.open) dialog.showModal();
  try {
    const detail = await api("/api/detail?" + new URLSearchParams(params));
    if (serial !== detailRequest || !dialog.open) return;
    detailMemoryId = detail.node.memory_id;
    $("detail-edit").classList.toggle("hidden", !detailMemoryId);
    const n = detail.node;
    currentDetail = detail;
    $("detail-title").textContent = SOURCE_LABEL[n.source_type] || "记忆详情";
    const memberships = detail.memberships.map((m) => `<div class="reason-row"><div class="reason-title">${esc(m.reason_text)}</div>
      <div class="weights">${weightBar("召回权重", m.recall_strength)}${weightBar("引用权重", m.citation_strength, true)}</div>
      <div class="muted" style="margin-top:10px">该理由下：展示 ${m.usage.presented || 0} 次 · 引用 ${m.usage.cited || 0} 次</div>
      ${detail.valid ? `<div class="row reason-actions"><button class="btn" data-correct="reason" data-chain="${esc(m.id)}">纠正理由</button><button class="btn" data-correct="reject_reason" data-chain="${esc(m.id)}">拒绝理由</button></div>` : ""}</div>`).join("");
    const edges = detail.edges.map((e) => `<div class="edge"><div class="row"><span class="tag">${esc(RELATION_LABEL[e.relation_type] || e.relation_type)}</span><span class="muted">置信度 ${Math.round(e.confidence * 100)}%</span></div>
      <p>${esc(e.reason_text)}</p><button class="btn" data-node="${esc(e.other.id)}">打开${esc(SOURCE_LABEL[e.other.source_type] || "记忆")} ↗</button>
      <div class="quote">${esc(e.other.content)}</div>${e.evidence.map((q) => `<div class="quote"><span class="muted">${q.node_id === n.id ? "本条依据" : "对方依据"}</span><br>${esc(q.quote)}</div>`).join("")}</div>`).join("");
    const roots = (detail.roots.length ? detail.roots : detail.sources).map((s) => `<div class="source-box">
      <div class="muted">${esc(s.source_type || s.role || s.kind || "来源")} · ${esc(s.actor_name || s.actor_id || s.source_ref || s.id || "")} · ${esc(fmtTs(s.ts || s.created_at))}</div>
      <div class="content">${esc(s.content || "")}</div>${s.record ? jsonDisclosure("工具经历与回执", s.record) : ""}</div>`).join("");
    const snapshots = detail.snapshots.map((s) => {
      const protectedHere = s.data.protected.includes(n.id);
      return `<div class="reason-row"><div class="row"><span class="tag">${protectedHere ? "Top" + (n.source_type === "raw" ? 3 : 1) + " 留存" : "转入冷层"}</span><span class="muted">任务 #${s.job_id} · ${s.applied ? "已应用" : "已冻结"} · ${fmtTs(s.created_at)}</span></div>
        <p>冻结权重 ${Number(s.data.scores[n.id] || 0).toFixed(4)} · 同批 ${s.data.node_ids.length} 个来源</p>${jsonDisclosure("查看冻结快照", s)}</div>`;
    }).join("");
    const history = (detail.corrections || []).map((c) => `<div class="correction-history"><span class="tag">${c.kind === "fact" ? "事实纠正" : c.kind === "reason" ? "理由纠正" : "理由已拒绝"}</span><span class="muted">${esc(fmtTs(c.created_at))}</span>
      ${c.quote ? `<div class="quote correction-old">${esc(c.quote)}</div>` : ""}
      ${c.replacement ? `<div class="quote">${esc(c.replacement)}</div>` : ""}
      <p>${esc(c.explanation)}</p>${jsonDisclosure("查看纠正前的完整记录", c.before_data)}</div>`).join("");
    const contexts = (detail.contexts || []).map((c) => `<div class="reason-row"><p>查询：${esc(c.query)}</p>${detail.memberships.filter((m) => m.id in c.shares).map((m) => weightBar(esc(m.reason_text), c.shares[m.id])).join("")}</div>`).join("");
    const siblings = (detail.event_siblings || []).map((s) => `<button class="btn" data-node="${esc(s.id)}">${esc(SOURCE_LABEL[s.source_type] || s.source_type)} · 查看同事件记录 ↗</button>`).join("");
    $("detail-body").innerHTML = `<div class="detail-meta"><span class="tag">${esc(scopeLabel(n.scope))}</span>${badge(n.retrieval_status)}<span class="tag">${n.ready ? "建联判断完成" : "等待建联"}</span>${!detail.valid ? '<span class="tag badge-failed">来源版本已变化</span>' : ""}</div>
      <div class="muted">来源 ${esc(n.source_ref)} · 版本 ${n.revision}</div>
      <div class="detail-section"><div class="section-heading"><h3>完整记忆</h3>${detail.valid && n.id ? '<button class="btn" data-correct="fact">纠正事实</button>' : ""}</div><div class="detail-original">${esc(detail.content)}</div>
      ${!detail.valid ? '<p class="muted">这条记忆正在等待纠错重建，当前正文仅供核对，已暂停召回。</p>' : ""}</div>
      <div class="detail-section"><h3>纠错历史</h3>${history || empty("尚无纠错记录")}</div>
      <div class="detail-section"><h3>关联理由与权重 <span class="muted">${detail.memberships.length}</span></h3>${neighborhood(detail)}${memberships || empty(n.ready ? "模型已完成判断，尚未建立有意义的关联" : "模型尚未处理这条记忆")}</div>
      <div class="detail-section"><h3>相邻记忆与建联依据</h3>${edges || empty("暂无关联边")}</div>
      <div class="detail-section"><h3>原始来源</h3>${roots || empty("暂无来源记录")}</div>
      <div class="detail-section"><h3>同事件证据</h3><p class="muted">同一回合的原话、回执、续接和摘要只算一份独立证据。</p><div class="row">${siblings || empty("未发现同事件的其他记录")}</div></div>
      <div class="detail-section"><h3>查询中的理由份额</h3><p class="muted">只有已核验的实际引用会学习份额；同一查询下，理由共同分配注意力。</p>${contexts || empty("尚无带关联理由的查询记录")}</div>
      <div class="detail-section"><h3>巩固与留存记录</h3>${snapshots || empty("这条记忆尚未经历巩固")}${n.retention_reason ? `<p class="muted">当前策略 ${esc(n.retention_reason)}</p>` : ""}</div>
      <div class="detail-section"><h3>召回与引用</h3>${detail.usage.length ? detail.usage.map((u) => `<p>${u.event_type === "cited" ? "已引用" : "已展示"} · ${fmtTs(u.created_at)} · ${esc(u.turn_id)}</p>`).join("") : empty("暂无真实召回或引用记录")}</div>
      <div class="detail-section"><h3>建联任务</h3>${jobTable(detail.jobs)}</div>
      <div class="detail-section">${jsonDisclosure("节点元数据与源文件信息", { node: n, sources: detail.sources })}</div>`;
  } catch (error) {
    if (serial === detailRequest) $("detail-body").innerHTML = empty(error.message);
    throw error;
  }
}

function jobNote(job) {
  if (job.last_error) return job.last_error;
  return [
    job.unresolved_count ? `${job.unresolved_count} 项关系补证后仍未确定，保留原文` : "",
    job.rejected_links ? `丢弃 ${job.rejected_links} 条不合格引用` : "",
  ].filter(Boolean).join("；") || "—";
}

function jobTable(items) {
  return items.length ? `<div class="table-scroll"><table><thead><tr><th>任务</th><th>类型</th><th>状态</th><th>尝试</th><th>执行耗时</th><th>更新时间</th><th>处理说明</th></tr></thead><tbody>${items.map((j) => `<tr><td>#${j.id}</td><td>${esc(JOB_LABEL[j.kind] || j.kind)}</td><td>${badge(j.status)}</td><td>${j.attempts}</td><td>${j.duration_seconds == null ? "—" : Number(j.duration_seconds).toFixed(1) + " 秒"}</td><td>${esc(fmtTs(j.updated_at))}</td><td>${esc(jobNote(j))}</td></tr>`).join("")}</tbody></table></div>` : empty("暂无任务");
}

function openCorrection(kind, chainId) {
  const detail = currentDetail;
  if (!detail?.valid || !detail.node.id) return;
  const chain = detail.memberships.find((m) => m.id === chainId);
  correctionRequest = { scope: detail.node.scope, node_id: detail.node.id,
    expected_digest: detail.node.digest, operation_id: crypto.randomUUID(), kind,
    ...(chain ? { chain_id: chain.id, expected_chain_revision: chain.revision } : {}) };
  $("correction-title").textContent = kind === "fact" ? "纠正事实" : kind === "reason" ? "纠正关联理由" : "拒绝关联理由";
  $("correction-help").textContent = kind === "fact" ? "原始消息和旧版本会保留。受影响的摘要与关联将重新评估。" : "旧关联会暂停使用，系统将依据纠正后的理由重新评估。";
  $("correction-form").reset();
  $("correction-quote-label").classList.toggle("hidden", kind !== "fact");
  $("correction-quote").required = kind === "fact";
  $("correction-replacement-label").classList.toggle("hidden", kind === "reject_reason");
  $("correction-replacement").required = kind !== "reject_reason";
  $("correction-replacement").value = chain?.reason_text || "";
  $("correction-error").textContent = "";
  $("correction-reload").classList.add("hidden");
  $("correction-editor").showModal();
  $(kind === "fact" ? "correction-quote" : kind === "reason" ? "correction-replacement" : "correction-explanation").focus();
}

$("detail-body").addEventListener("click", (event) => {
  const button = event.target.closest("[data-correct]");
  if (button) openCorrection(button.dataset.correct, button.dataset.chain);
});
$("correction-cancel").onclick = () => $("correction-editor").close();
$("correction-reload").onclick = () => {
  $("correction-editor").close();
  openMemoryDetail({ node_id: correctionRequest.node_id }).catch(alertError);
};
$("correction-form").onsubmit = async (event) => {
  event.preventDefault();
  const body = { ...correctionRequest, quote: $("correction-quote").value.trim(),
    replacement: $("correction-replacement").value.trim(), explanation: $("correction-explanation").value.trim() };
  $("correction-save").disabled = true;
  $("correction-error").textContent = "";
  try {
    const result = await api("/api/corrections", { method: "POST", body: JSON.stringify(body) });
    $("correction-editor").close();
    notify(`已保存纠正${result.repair_ids.length ? `，${result.repair_ids.length} 条派生记忆正在重建` : "，关联正在重新评估"}`);
    await openMemoryDetail({ node_id: body.node_id });
  } catch (error) {
    $("correction-error").textContent = error.message;
    $("correction-reload").classList.toggle("hidden", !/变更|版本/.test(error.message));
  } finally {
    $("correction-save").disabled = false;
  }
};

async function loadJobs() {
  const data = await api("/api/jobs?" + new URLSearchParams({ status: $("job-status").value, limit: 30 }));
  $("jobs-list").innerHTML = jobTable(data.items) + `<p class="muted">共 ${data.total} 个任务，显示最近 30 条。历史建联在后台逐批处理。</p>`;
}

$("n-search").onclick = () => { nodeOffset = 0; loadNodes().catch(alertError); };
$("n-keyword").onkeydown = (e) => { if (e.key === "Enter") $("n-search").click(); };
["n-scope", "n-type", "n-status", "n-ready"].forEach((id) => $(id).onchange = () => $("n-search").click());
$("n-prev").onclick = () => { nodeOffset = Math.max(0, nodeOffset - PAGE); loadNodes().catch(alertError); };
$("n-next").onclick = () => { if (nodeOffset + PAGE < nodeTotal) { nodeOffset += PAGE; loadNodes().catch(alertError); } };
[$("node-list"), $("detail-body")].forEach((el) => el.addEventListener("click", (event) => {
  const target = event.target.closest("[data-node]");
  if (target) openMemoryDetail({ node_id: target.dataset.node }).catch(alertError);
}));
$("detail-close").onclick = () => { detailRequest++; $("memory-detail").close(); };
$("detail-edit").onclick = () => { $("memory-detail").close(); openEditor(detailMemoryId).catch(alertError); };
$("job-status").onchange = () => loadJobs().catch(alertError);
document.querySelectorAll("nav button").forEach((button) => button.addEventListener("click", () => {
  if (button.dataset.tab === "overview") loadJobs().catch(alertError);
}));
setInterval(() => {
  if (document.hidden || $("memory-detail").open || $("editor").open) return;
  if (!$("tab-overview").classList.contains("hidden")) Promise.all([loadStats(), loadJobs()]).catch(alertError);
}, 15000);

// The constellation shares this complete detail view, including original evidence.
const detailLinkParams = new URLSearchParams(location.search);
const detailLink = Object.fromEntries(["node_id", "memory_id", "message_id", "scope"]
  .filter((key) => detailLinkParams.has(key)).map((key) => [key, detailLinkParams.get(key)]));
if (detailLink.node_id || detailLink.memory_id || detailLink.message_id) {
  openMemoryDetail(detailLink).catch(alertError);
}
