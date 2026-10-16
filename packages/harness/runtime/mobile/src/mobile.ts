import { AdbServerClient } from '@yume-chan/adb';
import type { Adb } from '@yume-chan/adb';
import { AdbScrcpyClient, AdbScrcpyOptions3_3_3 } from '@yume-chan/adb-scrcpy';
import { WebCodecsVideoDecoder } from '@yume-chan/scrcpy-decoder-webcodecs';
import { ReadableStream } from '@yume-chan/stream-extra';
import { WebSocketConnector } from './connector';
import { createVideoDecoder } from './video';

declare global {
  interface Window {
    mobileStudio: { canvas: HTMLCanvasElement; readonly ready: boolean; readonly serial: string; readonly connecting: boolean; disconnect(): Promise<void> };
    studioMediaChanged?: () => void;
    studioSourcesLocked?: () => boolean;
  }
}

const canvas = document.getElementById('phonecanvas') as HTMLCanvasElement;
const select = document.getElementById('phoneselect') as HTMLSelectElement;
const connectButton = document.getElementById('phoneconnect') as HTMLButtonElement;
const status = document.getElementById('phonestatus')!;
const hint = document.getElementById('phonehint')!;
let connector: WebSocketConnector | undefined;
let adb: Adb | undefined;
let client: AdbScrcpyClient<AdbScrcpyOptions3_3_3<true>> | undefined;
let decoder: WebCodecsVideoDecoder | undefined;
let abort: AbortController | undefined;
let serial = '';
let connecting = false;

function report(message: string) { status.textContent = message; }
async function json(url: string, body?: object) {
  const response = await fetch(url, body ? {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  } : undefined);
  const data = await response.json();
  if (!response.ok || data.error) throw new Error(data.error || `HTTP ${response.status}`);
  return data;
}
async function disconnect() {
  const oldClient = client, oldAdb = adb;
  client = undefined; adb = undefined; serial = '';
  abort?.abort(); abort = undefined;
  decoder?.dispose(); decoder = undefined;
  // Close ADB sockets first so a lost bridge cannot hold teardown indefinitely.
  connector?.close(); connector = undefined;
  await Promise.allSettled([oldClient?.close(), oldAdb?.close()]);
  canvas.width = 0; canvas.height = 0;
  hint.hidden = false;
  connectButton.textContent = '连接手机';
  window.studioMediaChanged?.();
}
window.mobileStudio = {
  canvas, get ready() { return !!decoder && decoder.framesRendered > 0; },
  get serial() { return serial; }, get connecting() { return connecting; }, disconnect,
};

async function refresh() {
  const config = await json('/api/mobile/config');
  if (!config.ws_url) throw new Error('手机未启用：以 --mobile 启动工作台，先运行 mobile/npm ci、npm run setup、npm run build');
  const inventoryConnector = new WebSocketConnector(config.ws_url);
  try {
    const devices = await new AdbServerClient(inventoryConnector).getDevices(['device', 'unauthorized', 'offline']);
    select.replaceChildren(new Option('不绑定手机（仅 PC）', ''));
    for (const device of devices) {
      const option = new Option(`${device.model || device.serial} · ${device.serial} · ${device.state}`, device.serial);
      option.disabled = device.state !== 'device';
      select.add(option);
    }
    if (config.serial && devices.some(d => d.serial === config.serial && d.state === 'device')) select.value = config.serial;
    connectButton.disabled = !!window.studioSourcesLocked?.();
    report(devices.length ? '选择已授权的 Android 手机' : '未发现手机：连接 USB 并开启 USB 调试');
  } finally { inventoryConnector.close(); }
}

async function connect() {
  if (!WebCodecsVideoDecoder.isSupported) throw new Error('手机串流需要支持 WebCodecs 的浏览器；请在本机 Chrome/Edge 打开工作台');
  const chosen = select.value;
  if (!chosen) {
    await json('/api/mobile/target', { serial: '' });
    await disconnect();
    (document.getElementById('layout') as HTMLSelectElement).value = 'pc';
    window.studioMediaChanged?.();
    report('SDK 仅绑定 PC；手机目标已取消');
    return;
  }
  const config = await json('/api/mobile/config');
  const response = await fetch('/mobile/scrcpy-server');
  if (!response.ok) throw new Error('缺少 scrcpy server：在 mobile 目录运行 npm run setup');
  const bytes = new Uint8Array(await response.arrayBuffer());
  await disconnect();
  connector = new WebSocketConnector(config.ws_url);
  const server = new AdbServerClient(connector);
  // Fail before starting a screen stream if the active agent owns another target.
  await json('/api/mobile/target', { serial: chosen });
  adb = await server.createAdb({ serial: chosen });
  const path = `/data/local/tmp/generalist-scrcpy-${config.server_version}.jar`;
  await AdbScrcpyClient.pushServer(adb, new ReadableStream<Uint8Array>({
    start(controller) { controller.enqueue(bytes); controller.close(); },
  }), path);
  client = await AdbScrcpyClient.start(adb, path, new AdbScrcpyOptions3_3_3({
    video: true, audio: false, control: false, tunnelForward: true,
    maxSize: 1920, maxFps: 30, videoBitRate: 8_000_000,
  }));
  const current = client;
  // Drain server logs to prevent the shell stream from blocking.
  void (async () => { for await (const line of current.output) console.debug('[scrcpy]', line); })().catch(() => {});
  const video = await current.videoStream;
  decoder = createVideoDecoder(video.metadata.codec, canvas);
  serial = chosen;
  hint.hidden = true;
  connectButton.textContent = '断开预览';
  const layout = document.getElementById('layout') as HTMLSelectElement;
  layout.value = (document.getElementById('video') as HTMLVideoElement).srcObject ? 'both' : 'mobile';
  abort = new AbortController();
  const connectedDecoder = decoder;
  const onEnd = async (error?: unknown) => {
    if (client !== current) return;
    await disconnect();
    report(error instanceof Error ? `手机串流结束：${error.message}` : '手机已断开；SDK 目标保留，可重连同一手机');
  };
  void video.stream.pipeTo(decoder.writable, { signal: abort.signal }).then(() => onEnd(), onEnd);
  void current.exited.then(() => onEnd(), onEnd);
  // Enable recording only after the first decoded frame is available.
  const firstFrame = () => {
    if (decoder !== connectedDecoder) return;
    if (!connectedDecoder.framesRendered) { setTimeout(firstFrame, 50); return; }
    report(`LIVE · ${chosen} · 30 FPS`);
    window.studioMediaChanged?.();
  };
  firstFrame();
}

document.getElementById('phonerefresh')!.onclick = () => { void refresh().catch(error => report(error.message)); };
connectButton.onclick = async () => {
  if (connecting || window.studioSourcesLocked?.()) return;
  connecting = true; connectButton.disabled = true;
  window.studioMediaChanged?.();
  try {
    if (client) { await disconnect(); report('预览已断开；SDK 目标保留'); }
    else { report('正在连接手机…'); await connect(); }
  } catch (error) { await disconnect(); report(error instanceof Error ? error.message : String(error)); }
  finally {
    connecting = false; connectButton.disabled = !!window.studioSourcesLocked?.();
    window.studioMediaChanged?.();
  }
};
window.addEventListener('pagehide', () => { connector?.close(); });
void refresh().catch(error => report(error.message));
