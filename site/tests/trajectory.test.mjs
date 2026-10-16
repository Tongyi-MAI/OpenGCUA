import test from 'node:test';
import assert from 'node:assert/strict';
import {deflateRawSync} from 'node:zlib';
import {defaultRun, matchesTask, stepDetails, frameLocation, unzipJSON} from '../src/trajectory-assets/core.mjs';
import {FramePlayer} from '../src/trajectory-assets/video.mjs';

function zip(raw) {
  const payload = Buffer.from(raw), packed = deflateRawSync(payload), name = Buffer.from('traj.json');
  let crc = 0xffffffff;
  for (const byte of payload) { crc ^= byte; for (let b = 0; b < 8; b++) crc = (crc >>> 1) ^ ((crc & 1) ? 0xedb88320 : 0); }
  crc = (crc ^ 0xffffffff) >>> 0;
  const local = Buffer.alloc(30), central = Buffer.alloc(46), end = Buffer.alloc(22);
  local.writeUInt32LE(0x04034b50); local.writeUInt16LE(8, 8); local.writeUInt16LE(name.length, 26);
  central.writeUInt32LE(0x02014b50); central.writeUInt16LE(8, 10); central.writeUInt32LE(crc, 16);
  central.writeUInt32LE(packed.length, 20); central.writeUInt32LE(payload.length, 24); central.writeUInt16LE(name.length, 28);
  end.writeUInt32LE(0x06054b50); end.writeUInt16LE(1, 10); end.writeUInt32LE(local.length + name.length + packed.length, 16);
  const bytes = Buffer.concat([local, name, packed, central, name, end]);
  return bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.length);
}

test('real DEFLATE ZIP parsing verifies CRC and rejects corruption', async () => {
  const buffer = zip('{"001":{"traj":[]},"_meta":{"fps":2}}');
  assert.equal((await unzipJSON(buffer))._meta.fps, 2);
  const corrupt = buffer.slice(0), data = new DataView(corrupt), central = data.getUint32(data.byteLength - 6, true);
  data.setUint32(central + 16, 0, true);
  await assert.rejects(unzipJSON(corrupt), /checksum/);
  await assert.rejects(unzipJSON(new ArrayBuffer(3)), /Invalid/);
});

test('noncontiguous screenshots and split-video boundaries map to correct timestamps', () => {
  const meta = {fps: 2, total_frames: 12, video_parts: [{file:'a.mp4',start_frame:0,frame_count:5},{file:'b.mp4',start_frame:5,frame_count:7}]};
  assert.equal(frameLocation(meta, 5).part.file, 'b.mp4');
  assert.equal(frameLocation(meta, 5).time, .125);
  assert.equal(frameLocation(meta, 11).time, 3.125);
  assert.throws(() => frameLocation(meta, 12), /Invalid/);
  assert.throws(() => frameLocation(meta, -1), /Invalid/);
});

test('empty, unscored and retried records retain their distinct meanings', () => {
  const task = {id:'001',instruction:'Calendar',default_run:'last',runs:[{key:'best',score:1},{key:'last',score:0,steps:0}]};
  assert.equal(defaultRun(task).score, 0);
  assert.equal(matchesTask(task, 'calendar', 'zero'), true);
  assert.equal(matchesTask(task, '', 'unscored'), false);
  assert.equal(stepDetails({action:{code:'<script>example</script>'}}, 'action'), '<script>example</script>');
});

class Video extends EventTarget {
  constructor() { super(); this.duration = 10; this.readyState = 2; this.time = 0; this.seeks = []; }
  canPlayType() { return 'probably'; }
  load() { if (this.src) queueMicrotask(() => this.dispatchEvent(new Event('loadedmetadata'))); }
  get currentTime() { return this.time; }
  set currentTime(value) { this.time = value; this.seeks.push(value); queueMicrotask(() => this.dispatchEvent(new Event('seeked'))); }
  pause() {}
  removeAttribute() { this.src = ''; }
}
const meta = {fps:2,total_frames:20,video_file:'traj.mp4',video_codec:'av1',display_width:1280,display_height:720};

test('latest frame wins and obsolete queued requests never paint', async () => {
  const video = new Video(), painted = [], canvas = {getContext: () => ({drawImage: v => painted.push(v.currentTime)})};
  const player = new FramePlayer(meta, 'https://example.org/model/', () => video);
  const first = player.draw(canvas, 0), last = player.draw(canvas, 8);
  assert.equal(await first, false);
  assert.equal(await last, true);
  assert.deepEqual(painted, [4.125]);
  assert.equal(video.preload, 'metadata');
  assert.equal(canvas.width, 1280);
  player.dispose();
});

test('failed media loads can retry and disposal cancels an active request', async () => {
  const videos = [];
  const player = new FramePlayer(meta, 'https://example.org/model/', () => {
    const video = new Video(); videos.push(video);
    if (videos.length === 1) video.load = () => queueMicrotask(() => video.dispatchEvent(new Event('error')));
    return video;
  });
  const canvas = {getContext: () => ({drawImage() {}})};
  await assert.rejects(player.draw(canvas, 0), /unavailable/);
  assert.equal(await player.draw(canvas, 1), true);
  const video = videos.at(-1);
  video.readyState = 0;
  Object.defineProperty(video, 'currentTime', {get: () => 0, set() {}});
  const pending = player.draw(canvas, 5);
  await new Promise(resolve => setImmediate(resolve));
  player.dispose();
  await assert.rejects(pending, {name:'AbortError'});
});
