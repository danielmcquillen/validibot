export interface ClipboardEnvironment {
    document: Document;
    navigator: Navigator;
}

function browserClipboardEnvironment(): ClipboardEnvironment {
    return {
        document,
        navigator,
    };
}

export function copyWithExecCommand(value: string, doc: Document = document): boolean {
    if (typeof doc.execCommand !== 'function') {
        return false;
    }

    const mount = doc.body ?? doc.documentElement;
    if (!mount) {
        return false;
    }

    const temporaryField = doc.createElement('textarea');
    temporaryField.value = value;
    temporaryField.setAttribute('readonly', '');
    temporaryField.style.position = 'absolute';
    temporaryField.style.left = '-9999px';
    mount.appendChild(temporaryField);
    temporaryField.focus();
    temporaryField.select();

    try {
        return doc.execCommand('copy');
    } catch {
        return false;
    } finally {
        temporaryField.remove();
    }
}

export async function copyTextToClipboard(
    value: string,
    environment: ClipboardEnvironment = browserClipboardEnvironment(),
): Promise<boolean> {
    if (!value) {
        return false;
    }

    const clipboard = environment.navigator.clipboard;
    if (clipboard && typeof clipboard.writeText === 'function') {
        try {
            await clipboard.writeText(value);
            return true;
        } catch {
            // Fall through to execCommand for browsers that expose the modern
            // API but reject it outside a secure context or permission grant.
        }
    }

    return copyWithExecCommand(value, environment.document);
}
