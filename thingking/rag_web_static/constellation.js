/* Local, complete graph view. Only real backend nodes and edges are drawn. */
(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const canvas = $("sky"), ctx = canvas.getContext("2d", { alpha: false });
  const LABEL = { raw: "原始对话", episode: "情节摘要", semantic: "长期事实", diary: "日记", excerpt: "对话片段" };
  const COLOR = { raw: "#bfd8e6", episode: "#dfb986", semantic: "#83d4b5", diary: "#c1b1df", excerpt: "#8ab7e2" };
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const clamp = (v, low, high) => Math.max(low, Math.min(high, v));
  const pct = (v) => `${(clamp(Number(v) || 0, 0, 1) * 100).toFixed(1)}%`;
  const scopeLabel = (scope) => {
    if (scope === "owner") return "管理员 / 本机";
    const parts = scope.split(":");
    if (parts[0] === "qq" && parts.length === 4) return `${parts[2] === "group" ? "QQ群" : "QQ私聊"} ${parts[3]}`;
    if (scope.startsWith("qq_group:")) return "QQ群 " + scope.slice(9);
    if (scope.startsWith("qq_private:")) return "QQ私聊 " + scope.slice(11);
    return scope;
  };
  const dateLabel = (ts) => ts ? new Date(ts * 1000).toLocaleString("zh-CN", { hour12: false }) : "";
  const reduced = matchMedia("(prefers-reduced-motion: reduce)").matches;
  const defaultInspector = $("inspector-body").innerHTML;
  let graph = null, nodes = [], edges = [], byId = new Map(), chainMap = new Map(), scopePositions = [];
  let visibleScopes = new Set();
  let worker = null, loadRun = 0, detailRun = 0, layoutDone = false;
  let selected = null, hovered = null, focusChain = null, activeChains = new Set(), highlighted = new Set();
  let view = { x: 0, y: 0, scale: 1 }, targetView = { ...view }, width = 0, height = 0;
  let frame = 0, previous = 0, pendingFit = false, layoutChanged = false, showResults = false, resultLimit = 40;
  let hitGrid = new Map(), drag = null, detailCache = new Map();
  const sprites = new Map(), controller = { current: null };

  async function api(url, signal) {
    const response = await fetch(url, { signal, cache: "no-store" });
    const data = await response.json();
    if (!response.ok || !data.success) throw new Error(data.error || `读取失败 (${response.status})`);
    return data.result;
  }
  function announce(text) { $("announcement").textContent = text; }
  function glow(color) {
    if (sprites.has(color)) return sprites.get(color);
    const sprite = document.createElement("canvas"); sprite.width = sprite.height = 96;
    const c = sprite.getContext("2d"), gradient = c.createRadialGradient(48, 48, 0, 48, 48, 48);
    gradient.addColorStop(0, "#ffffff"); gradient.addColorStop(.035, "#f1fbfc");
    gradient.addColorStop(.10, color + "dd"); gradient.addColorStop(.25, color + "60");
    gradient.addColorStop(.52, color + "16"); gradient.addColorStop(1, color + "00");
    c.fillStyle = gradient; c.fillRect(0, 0, 96, 96); sprites.set(color, sprite); return sprite;
  }
  function requestDraw() {
    if (!frame && !document.hidden) frame = requestAnimationFrame(draw);
  }
  function point(node) { return { x: node.x * view.scale + view.x, y: node.y * view.scale + view.y }; }
  function resize() {
    const rect = canvas.getBoundingClientRect(), dpr = Math.min(devicePixelRatio || 1, 2);
    const oldWidth = width, oldHeight = height;
    width = rect.width; height = rect.height;
    canvas.width = Math.round(width * dpr); canvas.height = Math.round(height * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    if (oldWidth) {
      view.x += (width - oldWidth) / 2; view.y += (height - oldHeight) / 2;
      targetView.x += (width - oldWidth) / 2; targetView.y += (height - oldHeight) / 2;
    }
    if (nodes.length && !oldWidth) fit(false);
    requestDraw();
  }
  function fit(animated = true, ids = null) {
    const items = nodes.filter((n) => n.visible && (!ids || ids.has(n.id)));
    if (!items.length || !Number.isFinite(items[0].x)) return;
    let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
    items.forEach((n) => { minX = Math.min(minX, n.x); maxX = Math.max(maxX, n.x); minY = Math.min(minY, n.y); maxY = Math.max(maxY, n.y); });
    const top = width < 500 ? 300 : 260, bottom = 110;
    const scale = clamp(Math.min((width - 110) / Math.max(160, maxX - minX), (height - top - bottom) / Math.max(160, maxY - minY)), .015, 3);
    targetView = { scale, x: width / 2 - (minX + maxX) / 2 * scale, y: top + (height - top - bottom) / 2 - (minY + maxY) / 2 * scale };
    if (!animated || reduced) view = { ...targetView };
    requestDraw();
  }
  function zoom(factor, x = width / 2, y = height * .58) {
    const scale = clamp(targetView.scale * factor, .015, 6), ratio = scale / targetView.scale;
    targetView = { scale, x: x - (x - targetView.x) * ratio, y: y - (y - targetView.y) * ratio };
    if (reduced) view = { ...targetView };
    requestDraw();
  }
  function centerNode(node) {
    const scale = clamp(Math.max(targetView.scale, .85), .015, 3);
    targetView = { scale, x: width / 2 - node.x * scale, y: height * .58 - node.y * scale };
    if (reduced) view = { ...targetView };
    requestDraw();
  }
  function rebuildHits() {
    hitGrid = new Map();
    nodes.forEach((n) => {
      if (!n.visible || !Number.isFinite(n.x)) return;
      const p = point(n);
      if (p.x < -20 || p.x > width + 20 || p.y < -20 || p.y > height + 20) return;
      const key = `${Math.floor(p.x / 24)},${Math.floor(p.y / 24)}`;
      if (!hitGrid.has(key)) hitGrid.set(key, []);
      hitGrid.get(key).push(n);
    });
  }
  function hit(x, y) {
    const gx = Math.floor(x / 24), gy = Math.floor(y / 24);
    let best = null, distance = 11 * 11;
    for (let dx = -1; dx <= 1; dx++) for (let dy = -1; dy <= 1; dy++) {
      for (const n of hitGrid.get(`${gx + dx},${gy + dy}`) || []) {
        const p = point(n), d = (p.x - x) ** 2 + (p.y - y) ** 2;
        if (d < distance) { best = n; distance = d; }
      }
    }
    return best;
  }
  function updateHighlight() {
    const anchor = hovered || selected;
    activeChains = new Set(focusChain ? [focusChain] : anchor?.chains || []);
    highlighted = new Set();
    activeChains.forEach((id) => (chainMap.get(id)?.members || []).forEach((member) => highlighted.add(member)));
    if (anchor) highlighted.add(anchor.id);
    const active = !!(anchor || focusChain);
    nodes.forEach((n) => {
      const weight = clamp(n.max_weight, 0, 1);
      n.targetAlpha = !active ? weight : highlighted.has(n.id) ? Math.min(1, .82 + weight * .18) : weight * .09;
      if (n === anchor) n.targetAlpha = 1;
    });
    edges.forEach((e) => { e.targetAlpha = !active ? .09 + e.confidence * .1 : activeChains.has(e.chain_id) ? .62 : .009; });
    canvas.dataset.activeChains = activeChains.size;
    canvas.dataset.highlightedNodes = [...highlighted].filter((id) => byId.get(id)?.visible).length;
    canvas.dataset.anchor = anchor?.id || "";
    canvas.dataset.focusChain = focusChain || "";
    requestDraw();
  }
  function draw(time) {
    frame = 0;
    const dt = Math.min(50, previous ? time - previous : 16); previous = time;
    const ease = reduced ? 1 : 1 - Math.exp(-dt / 65);
    let moving = false;
    ["x", "y", "scale"].forEach((key) => {
      const delta = targetView[key] - view[key];
      if (Math.abs(delta) > (key === "scale" ? .0001 : .03)) { view[key] += delta * ease; moving = true; }
      else view[key] = targetView[key];
    });
    nodes.forEach((n) => {
      if (!Number.isFinite(n.x)) return;
      if (layoutChanged && Number.isFinite(n.tx)) {
        const dx = n.tx - n.x, dy = n.ty - n.y;
        if (Math.abs(dx) + Math.abs(dy) > .05) { n.x += dx * ease; n.y += dy * ease; moving = true; }
        else { n.x = n.tx; n.y = n.ty; }
      }
      const delta = n.targetAlpha - n.alpha;
      if (Math.abs(delta) > .001) { n.alpha += delta * ease; moving = true; } else n.alpha = n.targetAlpha;
    });
    ctx.fillStyle = "#080f17"; ctx.fillRect(0, 0, width, height);
    const shade = ctx.createRadialGradient(width * .46, height * .56, 0, width * .46, height * .56, Math.max(width, height) * .6);
    shade.addColorStop(0, "#10212b"); shade.addColorStop(1, "#080f17");
    ctx.fillStyle = shade; ctx.fillRect(0, 0, width, height);
    edges.forEach((edge) => {
      const delta = edge.targetAlpha - edge.alpha;
      if (Math.abs(delta) > .001) { edge.alpha += delta * ease; moving = true; } else edge.alpha = edge.targetAlpha;
      const a = byId.get(edge.source), b = byId.get(edge.target);
      if (!a?.visible || !b?.visible || !Number.isFinite(a.x) || !Number.isFinite(b.x)) return;
      const p = point(a), q = point(b);
      if (Math.max(p.x, q.x) < 0 || Math.min(p.x, q.x) > width || Math.max(p.y, q.y) < 0 || Math.min(p.y, q.y) > height) return;
      ctx.globalAlpha = edge.alpha; ctx.lineWidth = activeChains.has(edge.chain_id) ? .9 : .55;
      ctx.strokeStyle = edge.relation === "contradicts" ? "#dbab8d" : "#87bea9";
      ctx.beginPath(); ctx.moveTo(p.x, p.y); ctx.lineTo(q.x, q.y); ctx.stroke();
    });
    ctx.globalAlpha = 1; ctx.font = '9px "Avenir Next", "PingFang SC", sans-serif'; ctx.textAlign = "center";
    scopePositions.forEach((group) => {
      if (!visibleScopes.has(group.scope)) return;
      const p = point(group); ctx.fillStyle = "#6f89968c"; ctx.fillText(scopeLabel(group.scope), p.x, p.y);
    });
    let drawn = 0;
    ctx.globalCompositeOperation = "screen";
    nodes.forEach((n) => {
      if (!n.visible || !Number.isFinite(n.x)) return;
      const p = point(n);
      if (p.x < -35 || p.y < -35 || p.x > width + 35 || p.y > height + 35) return;
      const size = (n.kind === "raw" ? 25 : 34) * clamp(Math.sqrt(view.scale), .58, 1.8);
      ctx.globalAlpha = n.alpha;
      ctx.drawImage(glow(COLOR[n.kind] || COLOR.raw), p.x - size / 2, p.y - size / 2, size, size);
      if (n === selected || n === hovered) {
        ctx.globalAlpha = .75; ctx.strokeStyle = COLOR[n.kind] || COLOR.raw; ctx.lineWidth = .6;
        ctx.beginPath(); ctx.arc(p.x, p.y, Math.max(7, size * .3), 0, Math.PI * 2); ctx.stroke();
      }
      drawn++;
    });
    ctx.globalAlpha = 1; ctx.globalCompositeOperation = "source-over";
    canvas.dataset.drawnNodes = drawn; canvas.dataset.layout = layoutDone && !moving ? "settled" : "settling";
    $("zoom-label").textContent = `${Math.round(view.scale * 100)}%`;
    rebuildHits();
    if (moving) requestDraw();
  }
  function updateTooltip(node, x, y) {
    const tip = $("tooltip");
    if (!node) { tip.hidden = true; return; }
    tip.innerHTML = `<small>${esc(LABEL[node.kind] || node.kind)} · 最大权重 ${pct(node.max_weight)}</small><p>${esc(node.preview.slice(0, 92))}${node.preview.length > 92 ? "…" : ""}</p><footer>${node.chains.length} 条关联链 · 点击固定查看原文</footer>`;
    tip.dataset.nodeId = node.id;
    tip.style.left = `${clamp(x + 17, 12, Math.max(12, width - 247))}px`;
    tip.style.top = `${clamp(y + 17, 12, Math.max(12, height - 165))}px`; tip.hidden = false;
  }
  function setHover(node, x, y) {
    if (hovered !== node) {
      hovered = node; updateHighlight();
      if (!selected && !focusChain) {
        // Keep the last preview available when the pointer moves into the inspector.
        if (node) renderInspector(node);
      }
    }
    canvas.style.cursor = drag ? "grabbing" : node ? "pointer" : "grab";
    updateTooltip(node, x, y);
  }
  function renderInspector(node, detail = null) {
    const color = COLOR[node.kind] || COLOR.raw, pinned = selected === node;
    const scope = scopeLabel(node.scope), basis = node.weight_basis === "chain_max" ? "各关联理由的召回 / 引用权重取最大值" : "独立节点使用当前初始权重";
    const chainCards = node.chains.map((id) => {
      const chain = chainMap.get(id), weights = chain.weights[node.id];
      return `<button class="chain-card" data-chain="${esc(id)}" aria-pressed="${focusChain === id}"><strong>${esc(chain.reason)}</strong><small><span>${chain.members.length} 条记忆${weights ? ` · 召回 ${pct(weights.recall)} / 引用 ${pct(weights.citation)}` : ""}</span><span>${focusChain === id ? "已聚焦" : "聚焦 ↗"}</span></small></button>`;
    }).join("");
    $("clear-node").hidden = !selected;
    $("inspector-body").innerHTML = `<div class="node-tags"><span class="kind-tag" style="--node-color:${color}">${esc(LABEL[node.kind] || node.kind)}</span><span>${esc(scope)}</span><span>${node.status === "cold" ? "冷层" : "活跃"}</span>${node.record_only ? "<span>审计记录</span>" : ""}${!node.registered ? "<span>尚未建联</span>" : ""}</div>
      <h2 class="node-title">${pinned ? "这束光，已被留住。" : "一条正在显现的记忆。"}</h2><p class="node-time">${esc(node.day_key || dateLabel(node.ts))}</p>
      <div class="weight-label"><span>最大权重 / 光斑亮度</span><strong>${pct(node.max_weight)}</strong></div><div class="weight-track"><i style="width:${pct(node.max_weight)}"></i></div><p class="weight-note">${basis}</p>
      <div class="node-content" id="node-content">${esc(detail?.content ?? node.preview)}${!detail && node.preview.length >= 280 ? "\n…" : ""}</div>
      ${!pinned ? '<button class="pin-button" id="pin-node">固定这条记忆 · 读取完整原文</button>' : ""}
      <a class="detail-link" id="detail-link" href="/?${esc(new URLSearchParams({ ...node.detail_params, scope: node.scope }).toString())}" target="_blank" rel="noopener">完整详情、原始依据与巩固记录 ↗</a>
      <div class="section-label"><span>相关理由链</span><span>${node.chains.length}</span></div>
      ${chainCards || `<p class="no-chains">${node.record_only ? "这条审计记录保留原文，不参与记忆检索。" : node.ready ? "已完成建联判断，暂未发现有依据的关联。" : "这条记忆尚未完成建联，原文已完整保留。"}</p>`}`;
    $("pin-node")?.addEventListener("click", () => selectNode(node));
  }
  async function selectNode(node, center = false) {
    if (!node) return;
    selected = node; hovered = null; detailRun++; $("tooltip").hidden = true;
    if (center) centerNode(node);
    $("results").hidden = true; showResults = false; updateHighlight();
    renderInspector(node, detailCache.get(node.id)); $("inspector-body").scrollTop = 0;
    announce(`已固定${LABEL[node.kind] || node.kind}，关联理由 ${node.chains.length} 条。`);
    if (detailCache.has(node.id)) return;
    const serial = detailRun, content = $("node-content"); content.setAttribute("aria-busy", "true");
    try {
      const detail = await api("/api/detail?" + new URLSearchParams({ ...node.detail_params, scope: node.scope }));
      if (serial !== detailRun || selected?.id !== node.id) return;
      detailCache.set(node.id, detail); renderInspector(node, detail);
      if (focusChain) appendChainMembers();
    } catch (error) {
      if (serial !== detailRun) return;
      content.removeAttribute("aria-busy");
      const note = document.createElement("p"); note.className = "no-chains"; note.textContent = `完整原文读取失败：${error.message}`;
      content.after(note); announce(note.textContent);
    }
  }
  function clearNode() {
    selected = null; hovered = null; detailRun++; $("clear-node").hidden = true; $("tooltip").hidden = true;
    if (focusChain) renderChainInspector(); else $("inspector-body").innerHTML = defaultInspector;
    updateHighlight();
  }
  function appendChainMembers() {
    $("chain-member-list")?.remove();
    const chain = chainMap.get(focusChain); if (!chain) return;
    const block = document.createElement("div"); block.id = "chain-member-list";
    const visible = chain.members.filter((id) => byId.get(id)?.visible);
    block.innerHTML = `<div class="section-label"><span>链中的记忆</span><span>${visible.length} / ${chain.members.length}</span></div>` + visible.map((id) => {
      const n = byId.get(id);
      return `<button class="chain-members" data-member="${esc(id)}"><small>${esc(LABEL[n.kind] || n.kind)} · 最大权重 ${pct(n.max_weight)}</small>${esc(n.preview.slice(0, 85))}${n.preview.length > 85 ? "…" : ""}</button>`;
    }).join("");
    $("inspector-body").append(block);
  }
  function renderChainInspector() {
    const chain = chainMap.get(focusChain); if (!chain) return;
    $("inspector-body").innerHTML = `<div class="node-tags"><span>${esc(scopeLabel(chain.scope))}</span><span>理由链</span></div><h2 class="node-title">循着这条关联。</h2><p class="node-content">${esc(chain.reason)}</p><p class="weight-note">亮起的是这条理由链中的全部成员。其他关联留在背景。</p>`;
    appendChainMembers();
  }
  function setChain(id) {
    focusChain = focusChain === id ? null : id;
    const chain = chainMap.get(focusChain);
    $("focus-banner").hidden = !chain;
    $("focus-reason").textContent = chain?.reason || "";
    if (selected) { renderInspector(selected, detailCache.get(selected.id)); if (chain) appendChainMembers(); }
    else if (chain) renderChainInspector(); else if (hovered) renderInspector(hovered); else $("inspector-body").innerHTML = defaultInspector;
    updateHighlight();
    if (chain) { fit(true, new Set(chain.members)); announce(`聚焦：${chain.reason}，包含 ${chain.members.length} 条记忆。`); }
  }
  function renderResults() {
    const query = $("search").value.trim();
    const matches = nodes.filter((n) => n.visible);
    const panel = $("results");
    panel.hidden = !showResults || !query;
    if (panel.hidden) return;
    panel.innerHTML = `<div class="results-meta">找到 ${matches.length} 条记忆 · 选择后定位</div>` + matches.slice(0, resultLimit).map((n) =>
      `<button class="result-row" data-result="${esc(n.id)}">${esc(n.preview.slice(0, 85))}<small>${esc(LABEL[n.kind] || n.kind)} · ${esc(scopeLabel(n.scope))} · ${pct(n.max_weight)}</small></button>`
    ).join("") + (matches.length > resultLimit ? '<button id="more-results" class="result-row">显示更多结果 ↓</button>' : "");
    $("more-results")?.addEventListener("click", () => { resultLimit += 40; renderResults(); });
  }
  function filter() {
    const query = $("search").value.trim().toLocaleLowerCase(), scope = $("scope").value, kind = $("kind").value;
    nodes.forEach((n) => { n.visible = (!scope || n.scope === scope) && (!kind || n.kind === kind) && (!query || n.searchText.includes(query)); });
    visibleScopes = new Set(nodes.filter((n) => n.visible).map((n) => n.scope));
    if (selected && !selected.visible) clearNode();
    if (hovered && !hovered.visible) hovered = null;
    if (focusChain && !chainMap.get(focusChain).members.some((id) => byId.get(id)?.visible)) setChain(focusChain);
    const visible = nodes.filter((n) => n.visible).length, visibleEdges = edges.filter((e) => byId.get(e.source).visible && byId.get(e.target).visible).length;
    $("visible-count").textContent = `显示 ${visible.toLocaleString()} / ${nodes.length.toLocaleString()} 条记忆 · ${visibleEdges} 根连线`;
    canvas.dataset.visibleNodes = visible;
    $("gesture-hint").textContent = !visible ? "没有符合筛选的记忆，试试其他关键词或作用域" : "悬浮点亮关联 · 点击固定记忆 · 滚动缩放 · 拖动画布";
    renderResults(); updateHighlight(); fit();
    if (selected) { renderInspector(selected, detailCache.get(selected.id)); if (focusChain) appendChainMembers(); }
    else if (focusChain) renderChainInspector();
  }
  async function load() {
    const run = ++loadRun;
    controller.current?.abort(); controller.current = new AbortController();
    const timeout = setTimeout(() => controller.current?.abort(), 65000);
    $("reload").disabled = true; $("retry").hidden = true; $("load-state").hidden = false;
    $("load-state").querySelector("strong").textContent = "让记忆显现";
    $("load-state").querySelector("p").textContent = "正在读取全库中的原文和关联…";
    $("live-state").textContent = "读取全库";
    try {
      const data = await api("/api/graph", controller.current.signal);
      if (run !== loadRun) return;
      worker?.terminate(); detailCache.clear(); detailRun++;
      graph = data; selected = hovered = focusChain = null; $("focus-banner").hidden = true; $("clear-node").hidden = true;
      $("inspector-body").innerHTML = defaultInspector;
      nodes = data.nodes.map((n) => ({ ...n, x: NaN, y: NaN, alpha: n.max_weight, targetAlpha: n.max_weight, visible: true, searchText: (n.search_text || n.preview).toLocaleLowerCase() }));
      edges = data.edges.map((e) => ({ ...e, alpha: .09 + e.confidence * .1, targetAlpha: .09 + e.confidence * .1 }));
      byId = new Map(nodes.map((n) => [n.id, n])); chainMap = new Map(data.chains.map((c) => [c.id, c]));
      const currentScope = $("scope").value;
      $("scope").innerHTML = '<option value="">全部作用域</option>' + data.scopes.map((s) => `<option value="${esc(s)}">${esc(scopeLabel(s))}</option>`).join("");
      $("scope").value = data.scopes.includes(currentScope) ? currentScope : "";
      $("node-count").textContent = data.counts.nodes.toLocaleString();
      $("edge-count").textContent = data.counts.edges.toLocaleString();
      $("chain-count").textContent = data.counts.chains.toLocaleString();
      $("snapshot-time").textContent = "全库快照 / " + dateLabel(data.generated_at);
      $("live-state").textContent = "已连接 · 全量快照";
      $("load-state").hidden = !!nodes.length; layoutDone = false; layoutChanged = false; pendingFit = true;
      $("load-state").querySelector(".loader").hidden = false;
      filter();
      if (!nodes.length) {
        $("load-state").querySelector("strong").textContent = "记忆尚未开始";
        $("load-state").querySelector("p").textContent = "记忆库中出现第一条记录后，它会在这里发光。";
        $("load-state").querySelector(".loader").hidden = true; return;
      }
      worker = new Worker("/constellation_worker.js");
      worker.onmessage = ({ data: layout }) => {
        if (run !== loadRun || layout.run !== run) return;
        scopePositions = layout.scopes;
        nodes.forEach((n, i) => {
          n.tx = layout.positions[i * 2]; n.ty = layout.positions[i * 2 + 1];
          if (!Number.isFinite(n.x)) { n.x = n.tx; n.y = n.ty; }
        });
        layoutChanged = true; layoutDone = layout.done;
        if (pendingFit) { fit(false); pendingFit = false; }
        if (layout.done) worker.terminate();
        requestDraw();
      };
      worker.onerror = () => {
        $("live-state").textContent = "布局读取失败";
        $("load-state").hidden = false;
        $("load-state").querySelector("strong").textContent = "布局未能完成";
        $("load-state").querySelector("p").textContent = "请重新连接以再次加载星图。";
        $("retry").hidden = false;
      };
      worker.postMessage({ nodes: nodes.map(({ id, scope }) => ({ id, scope })), edges: data.edges, run });
      announce(`全库已读取 ${nodes.length} 条记忆，${edges.length} 根真实关联连线。`);
    } catch (error) {
      if (run !== loadRun) return;
      $("live-state").textContent = "连接中断";
      $("load-state").querySelector("strong").textContent = "记忆库暂时没有回应";
      $("load-state").querySelector("p").textContent = error.name === "AbortError" ? "读取超时，请稍后重试。" : error.message;
      $("retry").hidden = false;
    } finally {
      clearTimeout(timeout); if (run === loadRun) $("reload").disabled = false;
    }
  }
  canvas.addEventListener("pointerdown", (event) => {
    if (event.button !== 0) return;
    canvas.focus({ preventScroll: true }); canvas.setPointerCapture(event.pointerId);
    drag = { pointer: event.pointerId, startX: event.clientX, startY: event.clientY, x: event.clientX, y: event.clientY, moved: false };
  });
  canvas.addEventListener("pointermove", (event) => {
    const rect = canvas.getBoundingClientRect(), x = event.clientX - rect.left, y = event.clientY - rect.top;
    if (drag && event.pointerId === drag.pointer) {
      drag.moved ||= Math.hypot(event.clientX - drag.startX, event.clientY - drag.startY) > 4;
      if (drag.moved) {
        targetView.x += event.clientX - drag.x; targetView.y += event.clientY - drag.y; view = { ...targetView };
        hovered = null; updateTooltip(null); updateHighlight(); canvas.style.cursor = "grabbing"; requestDraw();
      }
      drag.x = event.clientX; drag.y = event.clientY; return;
    }
    setHover(hit(x, y), x, y);
  });
  canvas.addEventListener("pointerup", (event) => {
    if (!drag || drag.pointer !== event.pointerId) return;
    const moved = drag.moved; drag = null;
    if (canvas.hasPointerCapture(event.pointerId)) canvas.releasePointerCapture(event.pointerId);
    if (!moved) { const rect = canvas.getBoundingClientRect(), node = hit(event.clientX - rect.left, event.clientY - rect.top); if (node) selectNode(node); else clearNode(); }
    canvas.style.cursor = "grab";
  });
  canvas.addEventListener("pointercancel", () => { drag = null; setHover(null); });
  canvas.addEventListener("pointerleave", () => { if (!drag) setHover(null); });
  canvas.addEventListener("wheel", (event) => {
    event.preventDefault(); const rect = canvas.getBoundingClientRect();
    zoom(Math.exp(-clamp(event.deltaY, -180, 180) * .002), event.clientX - rect.left, event.clientY - rect.top);
  }, { passive: false });
  canvas.addEventListener("keydown", (event) => {
    const moves = { ArrowLeft: [50, 0], ArrowRight: [-50, 0], ArrowUp: [0, 50], ArrowDown: [0, -50] };
    if (moves[event.key]) { event.preventDefault(); targetView.x += moves[event.key][0]; targetView.y += moves[event.key][1]; requestDraw(); }
    else if (event.key === "+" || event.key === "=") zoom(1.25);
    else if (event.key === "-") zoom(.8);
    else if (event.key.toLowerCase() === "f") fit();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") { if (focusChain) setChain(focusChain); else clearNode(); $("results").hidden = true; }
    if (event.key === "/" && !["INPUT", "SELECT", "TEXTAREA"].includes(event.target.tagName)) { event.preventDefault(); $("search").focus(); }
  });
  document.addEventListener("click", (event) => {
    if (!event.target.closest(".filter-bar, .search-results")) { $("results").hidden = true; showResults = false; }
  });
  $("inspector-body").addEventListener("click", (event) => {
    const chain = event.target.closest("[data-chain]"), member = event.target.closest("[data-member]");
    if (chain) setChain(chain.dataset.chain);
    if (member) selectNode(byId.get(member.dataset.member), true);
  });
  $("results").addEventListener("click", (event) => {
    const result = event.target.closest("[data-result]");
    if (result) selectNode(byId.get(result.dataset.result), true);
  });
  $("search").addEventListener("input", () => { showResults = true; resultLimit = 40; filter(); });
  $("search").addEventListener("focus", () => { showResults = true; renderResults(); });
  $("search").addEventListener("keydown", (event) => {
    if (event.key === "ArrowDown") { event.preventDefault(); $("results").querySelector("[data-result]")?.focus(); }
    if (event.key === "Enter") { const node = nodes.find((n) => n.visible); if (node) selectNode(node, true); }
  });
  ["scope", "kind"].forEach((id) => $(id).addEventListener("change", filter));
  $("clear-node").addEventListener("click", clearNode);
  $("clear-chain").addEventListener("click", () => setChain(focusChain));
  $("zoom-in").addEventListener("click", () => zoom(1.25));
  $("zoom-out").addEventListener("click", () => zoom(.8));
  $("fit").addEventListener("click", () => fit(true, focusChain ? new Set(chainMap.get(focusChain).members) : null));
  $("reload").addEventListener("click", load); $("retry").addEventListener("click", load);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) requestDraw(); });
  new ResizeObserver(resize).observe(canvas);
  resize(); load();
})();
