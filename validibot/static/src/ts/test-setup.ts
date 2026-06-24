// Vitest global setup for the front-end suite.
// ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
//
// jsdom does not reliably expose window.localStorage across versions (storage
// is origin-partitioned and the headless environment may not provide it). Our
// modules use localStorage for UI preferences, so we install a small in-memory
// implementation here. It behaves like the real Storage API closely enough for
// our tests (getItem/setItem/removeItem/clear) and is fully deterministic.
//
// Tests that want to simulate "localStorage throws" (privacy mode) spy on these
// methods with vi.spyOn — the stub exists as a real object on window, so the spy
// has something to replace.

class MemoryStorage implements Storage {
    private store = new Map<string, string>();

    get length(): number {
        return this.store.size;
    }

    clear(): void {
        this.store.clear();
    }

    getItem(key: string): string | null {
        return this.store.has(key) ? (this.store.get(key) as string) : null;
    }

    key(index: number): string | null {
        return Array.from(this.store.keys())[index] ?? null;
    }

    removeItem(key: string): void {
        this.store.delete(key);
    }

    setItem(key: string, value: string): void {
        this.store.set(key, String(value));
    }
}

// Install on window so `window.localStorage` and bare `localStorage` both work.
Object.defineProperty(window, 'localStorage', {
    value: new MemoryStorage(),
    writable: true,
    configurable: true,
});
