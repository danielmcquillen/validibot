/**
 * Vendor browser libraries required by the developer documentation.
 *
 * Production documentation must never fetch executable library code from a
 * public CDN. npm's lockfile pins the exact package and integrity digest; this
 * script copies only reviewed browser artifacts and their licenses into the
 * MkDocs/Zensical source tree so the generated site serves them itself.
 */

import {
    copyFileSync,
    mkdirSync,
    readFileSync,
} from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const repositoryRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const projectPackage = JSON.parse(
    readFileSync(resolve(repositoryRoot, "package.json"), "utf8"),
);
const mermaidPackage = JSON.parse(
    readFileSync(
        resolve(repositoryRoot, "node_modules/mermaid/package.json"),
        "utf8",
    ),
);
const expectedVersion = projectPackage.devDependencies.mermaid;

if (!/^\d+\.\d+\.\d+$/.test(expectedVersion)) {
    throw new Error(
        `Mermaid must use an exact version, received: ${expectedVersion}`,
    );
}
if (mermaidPackage.version !== expectedVersion) {
    throw new Error(
        "Installed Mermaid version does not match package.json. "
        + "Run npm ci before building documentation assets.",
    );
}

const vendorDirectory = resolve(
    repositoryRoot,
    "docs/dev_docs/javascripts/vendor",
);
mkdirSync(vendorDirectory, { recursive: true });
copyFileSync(
    resolve(repositoryRoot, "node_modules/mermaid/dist/mermaid.min.js"),
    resolve(vendorDirectory, "mermaid.min.js"),
);
copyFileSync(
    resolve(repositoryRoot, "node_modules/mermaid/LICENSE"),
    resolve(vendorDirectory, "MERMAID-LICENSE.txt"),
);

const fontDirectory = resolve(repositoryRoot, "docs/dev_docs/fonts");
mkdirSync(fontDirectory, { recursive: true });
const fontPackages = [
    {
        name: "@fontsource-variable/inter",
        files: [
            "inter-latin-ext-wght-normal.woff2",
            "inter-latin-ext-wght-italic.woff2",
            "inter-latin-wght-normal.woff2",
            "inter-latin-wght-italic.woff2",
        ],
        license: "INTER-OFL.txt",
    },
    {
        name: "@fontsource-variable/jetbrains-mono",
        files: [
            "jetbrains-mono-latin-ext-wght-normal.woff2",
            "jetbrains-mono-latin-ext-wght-italic.woff2",
            "jetbrains-mono-latin-wght-normal.woff2",
            "jetbrains-mono-latin-wght-italic.woff2",
        ],
        license: "JETBRAINS-MONO-OFL.txt",
    },
    {
        name: "@fontsource-variable/space-grotesk",
        files: [
            "space-grotesk-latin-ext-wght-normal.woff2",
            "space-grotesk-latin-wght-normal.woff2",
        ],
        license: "SPACE-GROTESK-OFL.txt",
    },
];

for (const fontPackage of fontPackages) {
    const packageVersion = projectPackage.devDependencies[fontPackage.name];
    if (!/^\d+\.\d+\.\d+$/.test(packageVersion)) {
        throw new Error(
            `${fontPackage.name} must use an exact version, received: `
            + packageVersion,
        );
    }
    const packageRoot = resolve(
        repositoryRoot,
        "node_modules",
        fontPackage.name,
    );
    const installedPackage = JSON.parse(
        readFileSync(resolve(packageRoot, "package.json"), "utf8"),
    );
    if (installedPackage.version !== packageVersion) {
        throw new Error(
            `${fontPackage.name} does not match package.json. `
            + "Run npm ci before building documentation assets.",
        );
    }
    for (const file of fontPackage.files) {
        copyFileSync(
            resolve(packageRoot, "files", file),
            resolve(fontDirectory, file),
        );
    }
    copyFileSync(
        resolve(packageRoot, "LICENSE"),
        resolve(fontDirectory, fontPackage.license),
    );
}

copyFileSync(
    resolve(repositoryRoot, "validibot/static/images/favicons/favicon.png"),
    resolve(repositoryRoot, "docs/dev_docs/images/favicon.png"),
);

console.log(
    `Vendored Mermaid ${expectedVersion}, fonts, and favicon for developer documentation.`,
);
