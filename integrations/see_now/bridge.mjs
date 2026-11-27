/** Local-only RGB24 -> PNG bridge. stdout is a bounded JSON-lines protocol. */
import { once } from 'node:events';
import { createPipeline } from '../../vendor/see_now/src/pipeline.js';

const MAX_HEADER = 8192;
const MAX_PIXELS = 2048 * 2048;
const MAX_OUTPUT = 24 * 1024 * 1024;
const numericOptions = {
  maxFrames: [1, 16, true], maxWaitSec: [1, 120], silentFloorSec: [1, 120],
  queueDepth: [0, 1, true],
  integralSeconds: [0.1, 120], cutAbs: [0, 255], dupMin: [0, 255],
  markMinGap: [0.01, 30], floorGapK: [0, 10], floorMin: [0.1, 120],
  floorMax: [1, 120], cutFutureDelay: [0, 1],
  cutSpike: [0.1, 10], cutMcAbs: [0, 255], cutBgAbs: [0, 255], cutIsolatedAbs: [0, 255],
};
const enumOptions = {
  strategy: ['native', 'standard', 'full', 'compact', 'single'],
  tierMode: ['density', 'weight'], packMode: ['rows', 'skyline'],
  layout: ['grid3', 'adaptive'],
};

function number(value, min, max, integer = false) {
  if (typeof value !== 'number' || !Number.isFinite(value) || value < min || value > max
      || (integer && !Number.isInteger(value))) throw new Error('invalid numeric parameter');
  return value;
}

function optionsOf(raw = {}) {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) throw new Error('invalid options');
  const options = { maxFrames: 9, layout: 'grid3', audioStrip: false, queueDepth: 1, maxCrop: 0, changeLog: false };
  for (const [key, value] of Object.entries(raw)) {
    if (numericOptions[key]) options[key] = number(value, ...numericOptions[key]);
    else if (enumOptions[key]?.includes(value)) options[key] = value;
    else if (['oneShot', 'scoreNormalize'].includes(key) && typeof value === 'boolean') options[key] = value;
    else if (Object.hasOwn(options, key) && ['audioStrip', 'queueDepth', 'maxCrop', 'changeLog'].includes(key)
             && value === options[key]) continue;
    else throw new Error(`unsupported or invalid pipeline option: ${key.slice(0, 48)}`);
  }
  if (options.layout === 'grid3' && options.maxFrames > 9) throw new Error('grid3 supports at most 9 frames');
  return options;
}

async function emit(message) {
  const line = JSON.stringify(message) + '\n';
  if (Buffer.byteLength(line) > MAX_OUTPUT) throw new Error('output exceeds size limit');
  if (!process.stdout.write(line)) await once(process.stdout, 'drain');
}

// Read exactly one bounded packet without repeatedly concatenating a full RGB frame.
const input = process.stdin[Symbol.asyncIterator]();
let chunk = Buffer.alloc(0);
let offset = 0;
async function readExactly(length, allowEof = false) {
  const result = Buffer.allocUnsafe(length);
  let filled = 0;
  while (filled < length) {
    if (offset === chunk.length) {
      const next = await input.next();
      if (next.done) {
        if (allowEof && filled === 0) return null;
        throw new Error('incomplete input packet');
      }
      chunk = next.value;
      offset = 0;
    }
    const size = Math.min(length - filled, chunk.length - offset);
    chunk.copy(result, filled, offset, offset + size);
    filled += size;
    offset += size;
  }
  return result;
}

let pipeline;
let frameBytes = 0;
let lastTime = -1;
let sequence = 0;
function serialize(sheet) {
  if (!sheet) return null;
  const { png, ...metadata } = sheet;
  const frames = sheet.frames;
  return {
    ...metadata, frames, sequence: ++sequence,
    wallFrom: frames[0]?.wall ?? sheet.wallFrom,
    wallTo: frames.at(-1)?.wall ?? sheet.wallTo,
    captured_at: (frames.at(-1)?.wall ?? sheet.wallTo) / 1000,
    png_base64: png.toString('base64'),
  };
}

async function main() {
  if (Number(process.versions.node.split('.')[0]) < 20) throw new Error('Node.js 20 or newer is required');
  while (true) {
    const prefix = await readExactly(4, true);
    if (!prefix) return;
    const length = number(prefix.readUInt32BE(), 2, MAX_HEADER, true);
    let header;
    try { header = JSON.parse((await readExactly(length)).toString('utf8')); }
    catch { throw new Error('invalid packet header'); }
    if (!header || typeof header !== 'object' || Array.isArray(header)) throw new Error('invalid packet header');
    const bytes = number(header.payload_bytes ?? 0, 0, MAX_PIXELS * 3, true);
    const id = header.id;
    if (id !== undefined) number(id, 1, Number.MAX_SAFE_INTEGER, true);
    if (header.op === 'init' && !pipeline && bytes === 0) {
      const width = number(header.width, 64, 2048, true);
      const height = number(header.height, 36, 2048, true);
      const fps = number(header.fps, 0.2, 30);
      if (width * height > MAX_PIXELS) throw new Error('frame exceeds size limit');
      frameBytes = width * height * 3;
      pipeline = createPipeline({ ...optionsOf(header.options), width, height, fps });
      await emit({ event: 'ready', id, width, height, fps, node: process.version });
    } else if (['frame', 'frame_sync'].includes(header.op) && pipeline && bytes === frameBytes) {
      const t = number(header.t, 0, Number.MAX_SAFE_INTEGER);
      const capturedAt = number(header.captured_at, 0, Number.MAX_SAFE_INTEGER);
      if (t < lastTime) throw new Error('frame timestamps must be ordered');
      lastTime = t;
      const buf = await readExactly(bytes);
      pipeline.push({ t, buf, wall: capturedAt * 1000 });
      pipeline.log.length = 0; // Offline diagnostic history is not needed by a long-running observer.
      const made = [];
      let sheet;
      while ((sheet = pipeline.take())) made.push(sheet);
      // Upstream checks the deadline when retaining a frame. A quiet screen after a
      // cut still needs its pending batch delivered on time, without manufacturing
      // periodic images when there is no pending batch.
      if (header.op === 'frame' && pipeline.secondsSinceWake !== null
          && pipeline.secondsSinceWake >= pipeline.constants.maxWaitSec) {
        const timed = pipeline.flushNow(t, 'time');
        if (timed) made.push(timed);
      }
      if (header.op === 'frame_sync') {
        await emit({ event: 'response', id, sheets: made.map(serialize) });
      } else {
        for (const produced of made) await emit({ event: 'sheet', sheet: serialize(produced) });
      }
    } else if (header.op === 'finish' && pipeline && bytes === 0) {
      pipeline.finish();
      const made = [];
      let sheet;
      while ((sheet = pipeline.take())) made.push(serialize(sheet));
      pipeline.log.length = 0;
      await emit({ event: 'response', id, sheets: made });
    } else if (header.op === 'flush' && pipeline && bytes === 0) {
      await emit({ event: 'response', id, sheet: serialize(pipeline.flushNow()) });
    } else if (header.op === 'close' && bytes === 0) {
      return;
    } else {
      throw new Error('invalid operation or payload size');
    }
  }
}

try {
  await main();
} catch (error) {
  // Never include raw input, frame bytes or a stack dump in application logs.
  const message = String(error?.message ?? 'bridge failed').slice(0, 160);
  try { await emit({ event: 'error', message }); } catch { /* parent may already have closed */ }
  process.exitCode = 1;
}
