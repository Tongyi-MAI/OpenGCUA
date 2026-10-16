import { AdbServerClient } from '@yume-chan/adb';
import { Consumable, ReadableStream, WritableStream } from '@yume-chan/stream-extra';
import type { MaybeConsumable } from '@yume-chan/stream-extra';

/** One WebSocket per ADB server connection, carrying the unmodified byte stream. */
export class WebSocketConnector implements AdbServerClient.ServerConnector {
  private sockets = new Set<WebSocket>();
  constructor(private url: string) {}

  async connect(options?: AdbServerClient.ServerConnectionOptions): Promise<AdbServerClient.ServerConnection> {
    const ws = new WebSocket(this.url);
    ws.binaryType = 'arraybuffer';
    this.sockets.add(ws);
    let resolveClosed!: (value: undefined) => void;
    const closed = new Promise<undefined>(resolve => { resolveClosed = resolve; });
    let ended = false;
    const readable = new ReadableStream<Uint8Array>({
      start: controller => {
        ws.addEventListener('message', event => {
          if (!ended) controller.enqueue(new Uint8Array(event.data));
        });
        ws.addEventListener('close', () => {
          if (!ended) { ended = true; controller.close(); }
          this.sockets.delete(ws);
          resolveClosed(undefined);
        });
        ws.addEventListener('error', () => {
          if (!ended) { ended = true; controller.error(new Error('Local ADB bridge disconnected')); }
        });
      },
      cancel: () => { ended = true; ws.close(); },
    });
    await new Promise<void>((resolve, reject) => {
      const timer = setTimeout(() => fail(new Error('Local ADB bridge timed out')), 10000);
      const cleanup = () => { clearTimeout(timer); options?.signal?.removeEventListener('abort', abort); };
      const fail = (error: Error) => { cleanup(); ws.close(); reject(error); };
      const abort = () => fail(new Error('ADB connection cancelled'));
      options?.signal?.addEventListener('abort', abort);
      ws.addEventListener('open', () => { cleanup(); resolve(); }, { once: true });
      ws.addEventListener('error', () => fail(new Error('Start the studio with --mobile to enable the local ADB bridge')), { once: true });
      ws.addEventListener('close', () => fail(new Error('Local ADB bridge closed')), { once: true });
      if (options?.signal?.aborted) abort();
    });
    const send = async (data: Uint8Array) => {
      if (ws.readyState !== WebSocket.OPEN) throw new Error('Local ADB bridge disconnected');
      ws.send(data);
      // Keep browser-side writes bounded when pushing the server JAR.
      while (ws.bufferedAmount > 1024 * 1024) {
        await new Promise(resolve => setTimeout(resolve, 10));
        if (ws.readyState !== WebSocket.OPEN) throw new Error('Local ADB bridge disconnected');
      }
    };
    return {
      readable,
      writable: new WritableStream<MaybeConsumable<Uint8Array>>({
        write: chunk => chunk instanceof Consumable ? chunk.tryConsume(send) : send(chunk),
        close: () => ws.close(), abort: () => ws.close(),
      }),
      closed,
      close: async () => { ws.close(); },
    };
  }

  addReverseTunnel(): never { throw new Error('Use scrcpy tunnelForward with the local ADB bridge'); }
  removeReverseTunnel(): void {}
  clearReverseTunnels(): void {}
  close(): void { for (const socket of this.sockets) socket.close(); this.sockets.clear(); }
}
