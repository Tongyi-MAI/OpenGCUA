// Local WebSocket transport for Tango's AdbServerClient. The host SDK and the
// browser share the existing ADB server; neither claims USB through WebUSB.
import http from 'node:http';
import net from 'node:net';
import { pathToFileURL } from 'node:url';
import { WebSocketServer, createWebSocketStream } from 'ws';

export function createBridge({ origin, adbPort = 5037 }) {
  const aliases = new Set([origin]);
  const parsedOrigin = new URL(origin);
  if (['127.0.0.1', 'localhost'].includes(parsedOrigin.hostname)) {
    parsedOrigin.hostname = parsedOrigin.hostname === 'localhost' ? '127.0.0.1' : 'localhost';
    aliases.add(parsedOrigin.origin);
  }
  const server = http.createServer((_req, res) => {
    res.writeHead(404).end();
  });
  const sockets = new Set();
  const wss = new WebSocketServer({ noServer: true, perMessageDeflate: false, maxPayload: 16 * 1024 * 1024 });
  server.on('upgrade', (req, socket, head) => {
    // Do not let arbitrary websites access local ADB through this bridge.
    if (req.url !== '/adb' || !aliases.has(req.headers.origin)) {
      socket.end('HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n');
      return;
    }
    wss.handleUpgrade(req, socket, head, ws => {
      const tcp = net.connect({ host: '127.0.0.1', port: adbPort });
      const duplex = createWebSocketStream(ws);
      sockets.add(tcp);
      tcp.on('close', () => { sockets.delete(tcp); duplex.destroy(); });
      tcp.on('error', () => duplex.destroy());
      duplex.on('error', () => tcp.destroy());
      duplex.on('close', () => tcp.destroy());
      tcp.pipe(duplex).pipe(tcp);
    });
  });
  return {
    server,
    close() {
      for (const socket of sockets) socket.destroy();
      for (const ws of wss.clients) ws.terminate();
      wss.close();
      server.close();
    },
  };
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const origin = process.env.DEMO_HUB_ORIGIN || 'http://127.0.0.1:8800';
  const bridge = createBridge({ origin, adbPort: Number(process.env.ANDROID_ADB_SERVER_PORT || 5037) });
  bridge.server.listen(Number(process.env.DEMO_MOBILE_PORT || 0), '127.0.0.1', () => {
    console.log(JSON.stringify({ ws_url: `ws://127.0.0.1:${bridge.server.address().port}/adb` }));
  });
  for (const signal of ['SIGINT', 'SIGTERM']) process.on(signal, () => bridge.close());
}
