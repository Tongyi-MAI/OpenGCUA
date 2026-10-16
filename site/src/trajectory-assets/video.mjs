import {frameLocation} from './core.mjs';

function mediaEvent(video, event, trigger, signal) {
  return new Promise((resolve, reject) => {
    const cleanup = () => {
      clearTimeout(timer);
      video.removeEventListener(event, done);
      video.removeEventListener('error', failed);
      signal.removeEventListener('abort', aborted);
    };
    const done = () => { cleanup(); resolve(); };
    const fail = error => { cleanup(); reject(error); };
    const failed = () => fail(new Error('The observation video is unavailable or could not be decoded.'));
    const aborted = () => fail(new DOMException('Canceled', 'AbortError'));
    const timer = setTimeout(() => fail(new Error('The screenshot took too long to load. Please retry.')), 30000);
    video.addEventListener(event, done, {once: true});
    video.addEventListener('error', failed, {once: true});
    signal.addEventListener('abort', aborted, {once: true});
    if (signal.aborted) { aborted(); return; }
    try { trigger(); } catch (error) { fail(error); }
  });
}

/** Range-backed video seeking; obsolete requests never paint a different step's frame. */
export class FramePlayer {
  constructor(meta, base, createVideo = () => document.createElement('video')) {
    this.meta = meta;
    this.base = base;
    this.createVideo = createVideo;
    this.videos = new Map();
    this.sequence = 0;
    this.queue = Promise.resolve();
    this.abort = new AbortController();
  }

  async video(part) {
    if (!this.videos.has(part.file)) {
      const video = this.createVideo();
      if (this.meta.video_codec === 'av1' && !video.canPlayType('video/mp4; codecs="av01.0.05M.08"')) {
        throw new Error('These observations use AV1. Open this page in an AV1-compatible Chrome, Edge, or Firefox browser.');
      }
      video.preload = 'metadata';
      video.muted = true;
      video.playsInline = true;
      video.crossOrigin = 'anonymous';
      const ready = mediaEvent(video, 'loadedmetadata', () => {
        video.src = new URL(part.file, this.base).href;
        video.load();
      }, this.abort.signal);
      this.videos.set(part.file, {video, ready});
      ready.catch(() => { if (this.videos.get(part.file)?.video === video) this.videos.delete(part.file); });
    }
    const entry = this.videos.get(part.file);
    await entry.ready;
    return entry.video;
  }

  draw(canvas, frame) {
    const sequence = ++this.sequence;
    const location = frameLocation(this.meta, frame);
    const job = this.queue.catch(() => {}).then(async () => {
      if (sequence !== this.sequence || this.abort.signal.aborted) return false;
      const video = await this.video(location.part);
      if (sequence !== this.sequence || this.abort.signal.aborted) return false;
      const time = Math.min(location.time, video.duration - 0.001);
      if (Math.abs(video.currentTime - time) > .001 || video.readyState < 2) {
        await mediaEvent(video, 'seeked', () => { video.currentTime = time; }, this.abort.signal);
      }
      if (video.readyState < 2) await mediaEvent(video, 'loadeddata', () => {}, this.abort.signal);
      if (sequence !== this.sequence || this.abort.signal.aborted) return false;
      canvas.width = this.meta.display_width || 1280;
      canvas.height = this.meta.display_height || 720;
      canvas.getContext('2d').drawImage(video, 0, 0, canvas.width, canvas.height);
      return true;
    });
    this.queue = job;
    return job;
  }

  cancelPending() { this.sequence++; }
  dispose() {
    this.cancelPending();
    this.abort.abort();
    for (const {video} of this.videos.values()) {
      video.pause();
      video.removeAttribute('src');
      video.load();
    }
    this.videos.clear();
  }
}
