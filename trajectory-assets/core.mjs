export function defaultRun(task) {
  return task.runs.find(r => r.key === task.default_run) || task.runs.at(-1);
}

export function matchesTask(task, search, filter) {
  const run = defaultRun(task);
  const score = run?.score;
  const result = filter === 'all' || (filter === 'unscored' ? score == null :
    filter === 'success' ? score >= 1 : filter === 'partial' ? score > 0 && score < 1 : score === 0);
  return result && `${task.id} ${task.instruction}`.toLowerCase().includes(search.toLowerCase().trim());
}

export function stepDetails(step, tab) {
  if (!step) return '';
  const show = value => typeof value === 'string' ? value : value ? JSON.stringify(value, null, 2) : '';
  if (tab === 'action') return show(step.action?.code ?? step.action ?? step.tool_call);
  if (tab === 'reasoning') return show(step.prediction);
  if (tab === 'feedback') return show(step.bash_result);
  return show(step.ask_user_response);
}

export function frameLocation(meta, frame) {
  if (!Number.isInteger(frame) || frame < 0 || frame >= meta.total_frames || !(meta.fps > 0)) {
    throw new RangeError('Invalid screenshot frame');
  }
  const parts = meta.video_parts || [{file: meta.video_file, start_frame: 0, frame_count: meta.total_frames}];
  const part = parts.find(p => frame >= p.start_frame && frame < p.start_frame + p.frame_count);
  if (!part || !/^[\w.-]+\.mp4$/.test(part.file)) throw new Error('Invalid video mapping');
  return {part, time: (frame - part.start_frame + 0.25) / meta.fps};
}

function crc32(bytes) {
  let crc = 0xffffffff;
  for (const byte of bytes) {
    crc ^= byte;
    for (let bit = 0; bit < 8; bit++) crc = (crc >>> 1) ^ ((crc & 1) ? 0xedb88320 : 0);
  }
  return (crc ^ 0xffffffff) >>> 0;
}

/** Read the single JSON member of a normal DEFLATE ZIP, without third-party code. */
export async function unzipJSON(buffer) {
  const view = new DataView(buffer), bytes = new Uint8Array(buffer);
  if (view.byteLength < 22) throw new Error('Invalid trajectory ZIP');
  let end = view.byteLength - 22;
  while (end >= Math.max(0, view.byteLength - 65557) && view.getUint32(end, true) !== 0x06054b50) end--;
  if (end < 0 || view.getUint32(end, true) !== 0x06054b50) throw new Error('Invalid ZIP directory');
  const central = view.getUint32(end + 16, true);
  if (view.getUint16(end + 10, true) !== 1 || central + 46 > bytes.length || view.getUint32(central, true) !== 0x02014b50) {
    throw new Error('Expected a single traj.json ZIP member');
  }
  const flags = view.getUint16(central + 8, true), method = view.getUint16(central + 10, true);
  const checksum = view.getUint32(central + 16, true), compressed = view.getUint32(central + 20, true);
  const size = view.getUint32(central + 24, true), nameLength = view.getUint16(central + 28, true);
  const local = view.getUint32(central + 42, true);
  const name = new TextDecoder().decode(bytes.subarray(central + 46, central + 46 + nameLength));
  if (name !== 'traj.json' || (flags & 1) || ![0, 8].includes(method) || size > 512 * 1024 * 1024 ||
      local + 30 > bytes.length || view.getUint32(local, true) !== 0x04034b50) throw new Error('Unsupported trajectory archive');
  const start = local + 30 + view.getUint16(local + 26, true) + view.getUint16(local + 28, true);
  if (start + compressed > central) throw new Error('Truncated trajectory ZIP');
  let content = bytes.subarray(start, start + compressed);
  if (method === 8) {
    let stream;
    try { stream = new DecompressionStream('deflate-raw'); }
    catch { throw new Error('ZIP decompression requires a current Chrome, Edge, Firefox, or Safari browser.'); }
    content = new Uint8Array(await new Response(new Blob([content]).stream().pipeThrough(stream)).arrayBuffer());
  }
  if (content.length !== size || crc32(content) !== checksum) throw new Error('Trajectory ZIP checksum mismatch');
  return JSON.parse(new TextDecoder('utf-8', {fatal: true}).decode(content));
}
