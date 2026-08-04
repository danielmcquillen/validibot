// Vitest global setup for the front-end suite.
// ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
//
// jsdom does not reliably expose Web Storage across versions (storage is
// origin-partitioned and the headless environment may not provide it). Our
// modules use localStorage and sessionStorage for UI preferences, so we install
// small in-memory implementations. They behave like the real Storage API
// closely enough for our tests and are fully deterministic.
//
// Tests that want to simulate storage throwing (privacy mode) spy on these
// methods with vi.spyOn — the stubs are real objects on window, so the spies
// have something to replace.

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

// Install on window so both qualified and bare Web Storage globals work.
Object.defineProperty(window, 'localStorage', {
    value: new MemoryStorage(),
    writable: true,
    configurable: true,
});

Object.defineProperty(window, 'sessionStorage', {
    value: new MemoryStorage(),
    writable: true,
    configurable: true,
});
