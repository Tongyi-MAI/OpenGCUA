import type { ScrcpyVideoCodecId } from '@yume-chan/scrcpy';
import { BitmapVideoFrameRenderer, WebCodecsVideoDecoder } from '@yume-chan/scrcpy-decoder-webcodecs';

export function createVideoDecoder(codec: ScrcpyVideoCodecId, canvas: HTMLCanvasElement) {
  return new WebCodecsVideoDecoder({ codec, renderer: new BitmapVideoFrameRenderer(canvas) });
}
