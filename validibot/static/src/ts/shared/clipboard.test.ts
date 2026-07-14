import { afterEach, describe, expect, it, vi } from 'vitest';

import { copyTextToClipboard, copyWithExecCommand } from './clipboard';

function buildDocumentWithExecCommand(result: boolean): {
    doc: Document;
    execCommand: ReturnType<typeof vi.fn>;
} {
    const doc = document.implementation.createHTMLDocument('clipboard test');
    const execCommand = vi.fn(() => result);
    Object.defineProperty(doc, 'execCommand', {
        value: execCommand,
        writable: true,
        configurable: true,
    });
    return { doc, execCommand };
}

describe('clipboard helpers', () => {
    afterEach(() => {
        vi.restoreAllMocks();
    });

    it('uses navigator.clipboard.writeText when available', async () => {
        const writeText = vi.fn(() => Promise.resolve());
        const { doc, execCommand } = buildDocumentWithExecCommand(true);
        const navigatorLike = {
            clipboard: { writeText },
        } as unknown as Navigator;

        const copied = await copyTextToClipboard('vbk_1_secret', {
            document: doc,
            navigator: navigatorLike,
        });

        expect(copied).toBe(true);
        expect(writeText).toHaveBeenCalledWith('vbk_1_secret');
        expect(execCommand).not.toHaveBeenCalled();
    });

    it('falls back to execCommand when the clipboard API rejects', async () => {
        const writeText = vi.fn(() => Promise.reject(new Error('denied')));
        const { doc, execCommand } = buildDocumentWithExecCommand(true);
        const navigatorLike = {
            clipboard: { writeText },
        } as unknown as Navigator;

        const copied = await copyTextToClipboard('vbk_1_secret', {
            document: doc,
            navigator: navigatorLike,
        });

        expect(copied).toBe(true);
        expect(execCommand).toHaveBeenCalledWith('copy');
        expect(doc.querySelector('textarea')).toBeNull();
    });

    it('returns false when neither clipboard strategy can copy', async () => {
        const doc = document.implementation.createHTMLDocument('clipboard test');
        const navigatorLike = {} as Navigator;

        const copied = await copyTextToClipboard('vbk_1_secret', {
            document: doc,
            navigator: navigatorLike,
        });

        expect(copied).toBe(false);
    });

    it('cleans up the temporary textarea when execCommand throws', () => {
        const doc = document.implementation.createHTMLDocument('clipboard test');
        Object.defineProperty(doc, 'execCommand', {
            value: vi.fn(() => {
                throw new Error('copy unavailable');
            }),
            writable: true,
            configurable: true,
        });

        const copied = copyWithExecCommand('vbk_1_secret', doc);

        expect(copied).toBe(false);
        expect(doc.querySelector('textarea')).toBeNull();
    });
});
