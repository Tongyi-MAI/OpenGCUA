// Download only during explicit setup. Runtime streaming stays local/offline.
import { mkdir, writeFile, rename } from 'node:fs/promises';
import { createHash } from 'node:crypto';
const version = '3.3.3';
const url = `https://github.com/Genymobile/scrcpy/releases/download/v${version}/scrcpy-server-v${version}`;
const response = await fetch(url);
if (!response.ok) throw new Error(`scrcpy download failed: ${response.status}`);
const bytes = new Uint8Array(await response.arrayBuffer());
if (bytes[0] !== 0x50 || bytes[1] !== 0x4b) throw new Error('Expected a scrcpy server JAR');
const digest = createHash('sha256').update(bytes).digest('hex');
if (digest !== '7e70323ba7f259649dd4acce97ac4fefbae8102b2c6d91e2e7be613fd5354be0') {
  throw new Error('scrcpy 3.3.3 server checksum mismatch');
}
await mkdir(new URL('vendor/', import.meta.url), { recursive: true });
const target = new URL(`vendor/scrcpy-server-v${version}`, import.meta.url);
await writeFile(new URL(`${target.href}.tmp`), bytes);
await rename(new URL(`${target.href}.tmp`), target);
console.log(`scrcpy ${version}: ${bytes.length} bytes, sha256 ${digest}`);
