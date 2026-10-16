// Run inside an existing Ubuntu X11 desktop after `mu-js setup linux`.
import { Runtime } from '../packages/javascript/src/index.mjs';
const runtime = new Runtime();
try {
  const resource = await runtime.add_linux({ name: 'desktop' });
  const mu = runtime.session({ output: value => {
    console.log(value.type === 'image' ? `Captured ${value.width}×${value.height} image` : value.text);
  }});
  const desktop = await mu.register(resource, { name: 'desktop' });
  await mu.emit(await desktop.ui.vision.observe());
  await mu.emit(await desktop.ui.ax.observe({ timeout_ms: 5000 }));
} finally { await runtime.close(); }
