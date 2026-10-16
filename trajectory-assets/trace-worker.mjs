import {unzipJSON} from './core.mjs';

let bundleURL, bundle, loading, controller;
self.onmessage = async ({data: {id, url, key, sha256}}) => {
  try {
    if (url !== bundleURL) {
      controller?.abort();
      controller = new AbortController();
      const signal = controller.signal;
      bundleURL = url;
      bundle = null;
      loading = (async () => {
        const response = await fetch(url, {signal});
        if (!response.ok) throw new Error('Trace files are not available yet. Please try again later.');
        const buffer = await response.arrayBuffer();
        if (sha256) {
          const hash = new Uint8Array(await crypto.subtle.digest('SHA-256', buffer));
          if (Array.from(hash, n => n.toString(16).padStart(2, '0')).join('') !== sha256) {
            throw new Error('The downloaded trace archive failed its integrity check.');
          }
        }
        const result = await unzipJSON(buffer);
        if (signal.aborted) throw new DOMException('Canceled', 'AbortError');
        return result;
      })();
    }
    const currentURL = url;
    const current = bundle || await loading;
    if (currentURL !== bundleURL) return;
    bundle = current;
    if (!bundle[key]) throw new Error('This attempt is missing from the trace archive.');
    self.postMessage({id, record: bundle[key], meta: bundle._meta});
  } catch (error) {
    if (url === bundleURL && !bundle) bundleURL = null;
    if (error.name !== 'AbortError') self.postMessage({id, error: error.message});
  }
};
