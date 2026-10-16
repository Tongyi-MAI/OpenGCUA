import { ProviderError } from "./errors.js";
/** Only this module depends on the pinned aria-ref selector implementation. */
export class Identity {
    page;
    tabId;
    sequence = 0;
    documents = new Map();
    targets = new Map();
    constructor(page, tabId) {
        this.page = page;
        this.tabId = tabId;
    }
    async document(frame) {
        const old = this.documents.get(frame);
        if (old) {
            try {
                if (await frame.evaluate((doc) => doc === document, old.document))
                    return old;
            }
            catch {
                /* document replaced */
            }
            for (const [id, target] of this.targets)
                if (target.frame === frame) {
                    this.targets.delete(id);
                    await target.handle.dispose().catch(() => { });
                }
            await old.document.dispose().catch(() => { });
        }
        const current = {
            id: `${this.tabId}:document:${++this.sequence}`,
            document: await frame.evaluateHandle(() => document),
        };
        this.documents.set(frame, current);
        return current;
    }
    async acquire(ref) {
        const handle = await this.page
            .locator(`aria-ref=${ref}`)
            .elementHandle({ timeout: 1000 });
        if (!handle)
            return undefined;
        const frame = await handle.ownerFrame();
        if (!frame) {
            await handle.dispose();
            return undefined;
        }
        const document = await this.document(frame);
        const candidates = [...this.targets.values()].filter((t) => t.frame === frame && t.document === document);
        // DOM identity, never name/role/position, reconciles a new Playwright reference.
        const index = candidates.length
            ? await handle.evaluate((node, known) => known.findIndex((old) => old === node), candidates.map((t) => t.handle))
            : -1;
        if (index >= 0) {
            await handle.dispose();
            return candidates[index];
        }
        const target = {
            id: `${document.id}:node:${++this.sequence}`,
            handle,
            frame,
            document,
        };
        this.targets.set(target.id, target);
        return target;
    }
    async resolve(id) {
        const target = this.targets.get(id);
        if (!target)
            throw new ProviderError("STALE_TARGET");
        try {
            if (target.frame.isDetached() ||
                !(await target.frame.evaluate((doc) => doc === document, target.document.document)) ||
                !(await target.handle.evaluate((node) => node.isConnected)))
                throw new Error("detached");
        }
        catch {
            this.targets.delete(id);
            await target.handle.dispose().catch(() => { });
            throw new ProviderError("STALE_TARGET");
        }
        return target;
    }
    async prune() {
        for (const id of [...this.targets.keys()])
            await this.resolve(id).catch(() => { });
    }
    async close() {
        await Promise.all([...this.targets.values()].map((t) => t.handle.dispose().catch(() => { })));
        this.targets.clear();
        this.documents.clear();
    }
}
