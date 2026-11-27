/* Stable, deterministic layout. Spatial buckets bound local repulsion work. */
"use strict";
let generation = 0;
const hash = (text) => {
  let value = 2166136261;
  for (let i = 0; i < text.length; i++) value = Math.imul(value ^ text.charCodeAt(i), 16777619);
  return value >>> 0;
};
self.onmessage = ({ data }) => {
  const run = ++generation, nodes = data.nodes, count = nodes.length;
  const index = new Map(nodes.map((n, i) => [n.id, i]));
  const edges = data.edges.map((e) => [index.get(e.source), index.get(e.target)]).filter(([a, b]) => a !== undefined && b !== undefined);
  const degree = new Uint32Array(count);
  edges.forEach(([a, b]) => { degree[a]++; degree[b]++; });
  const groups = new Map();
  nodes.forEach((n, i) => { if (!groups.has(n.scope)) groups.set(n.scope, []); groups.get(n.scope).push(i); });
  const x = new Float32Array(count), y = new Float32Array(count), ax = new Float32Array(count), ay = new Float32Array(count);
  const vx = new Float32Array(count), vy = new Float32Array(count), fx = new Float32Array(count), fy = new Float32Array(count);
  const scopes = [], golden = Math.PI * (3 - Math.sqrt(5));
  let cursor = 0;
  [...groups.entries()].sort(([a], [b]) => a === "owner" ? -1 : b === "owner" ? 1 : a.localeCompare(b)).forEach(([scope, items]) => {
    const radius = Math.max(130, Math.sqrt(items.length) * 19);
    const cx = cursor + radius, cy = hash(scope) % 100 - 50;
    cursor += 2 * radius + 170;
    const connected = items.filter((i) => degree[i]).sort((a, b) => degree[b] - degree[a] || hash(nodes[a].id) - hash(nodes[b].id));
    const isolated = items.filter((i) => !degree[i]).sort((a, b) => hash(nodes[a].id) - hash(nodes[b].id));
    const inner = Math.min(radius * .66, Math.sqrt(connected.length) * 20);
    connected.forEach((i, order) => {
      const r = Math.sqrt((order + .5) / Math.max(1, connected.length)) * inner;
      const angle = order * golden;
      x[i] = cx + Math.cos(angle) * r; y[i] = cy + Math.sin(angle) * r;
    });
    isolated.forEach((i, order) => {
      const start = connected.length ? inner + 48 : 0;
      const r = Math.sqrt(start * start + (radius * radius - start * start) * (order + .5) / Math.max(1, isolated.length));
      const angle = order * golden + hash(scope) / 100;
      x[i] = cx + Math.cos(angle) * r; y[i] = cy + Math.sin(angle) * r;
    });
    items.forEach((i) => { ax[i] = x[i]; ay[i] = y[i]; });
    scopes.push({ scope, x: cx, y: cy - radius - 42, radius });
  });
  const emit = (done) => {
    const positions = new Float32Array(count * 2);
    for (let i = 0; i < count; i++) { positions[i * 2] = x[i]; positions[i * 2 + 1] = y[i]; }
    self.postMessage({ positions, scopes, done, run: data.run }, [positions.buffer]);
  };
  emit(false);
  let iteration = 0;
  function step() {
    if (generation !== run) return;
    for (let batch = 0; batch < 6 && iteration < 156; batch++, iteration++) {
      fx.fill(0); fy.fill(0);
      const grid = new Map(), cell = 40;
      for (let i = 0; i < count; i++) {
        const key = `${Math.floor(x[i] / cell)},${Math.floor(y[i] / cell)}`;
        if (!grid.has(key)) grid.set(key, []);
        grid.get(key).push(i);
      }
      for (let i = 0; i < count; i++) {
        const gx = Math.floor(x[i] / cell), gy = Math.floor(y[i] / cell);
        let compared = 0;
        for (let dx = -1; dx <= 1; dx++) for (let dy = -1; dy <= 1; dy++) {
          const bucket = grid.get(`${gx + dx},${gy + dy}`) || [];
          for (const j of bucket) {
            if (compared >= 40) break;
            if (j === i) continue;
            compared++;
            const rx = x[i] - x[j], ry = y[i] - y[j], d2 = rx * rx + ry * ry + .1;
            if (d2 > 1600) continue;
            const force = Math.min(1.6, 80 / d2);
            fx[i] += rx * force; fy[i] += ry * force;
          }
        }
        fx[i] += (ax[i] - x[i]) * (degree[i] ? .002 : .018);
        fy[i] += (ay[i] - y[i]) * (degree[i] ? .002 : .018);
      }
      for (const [a, b] of edges) {
        const dx = x[b] - x[a], dy = y[b] - y[a], dist = Math.max(1, Math.hypot(dx, dy));
        const pull = (dist - 60) / dist * .014 / Math.sqrt(Math.max(degree[a], degree[b], 1));
        fx[a] += dx * pull; fy[a] += dy * pull; fx[b] -= dx * pull; fy[b] -= dy * pull;
      }
      const cooling = .6 * (1 - iteration / 190);
      for (let i = 0; i < count; i++) {
        vx[i] = (vx[i] + fx[i] * .16) * .7; vy[i] = (vy[i] + fy[i] * .16) * .7;
        x[i] += Math.max(-4, Math.min(4, vx[i])) * cooling;
        y[i] += Math.max(-4, Math.min(4, vy[i])) * cooling;
      }
    }
    if (iteration % 24 === 0 || iteration >= 156) emit(iteration >= 156);
    if (iteration < 156) setTimeout(step, 0);
  }
  step();
};
