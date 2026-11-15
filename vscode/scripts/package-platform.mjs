#!/usr/bin/env node
// One .vsix for one OS/CPU, with the frozen core from packaging/build_core.py inside bin/.
//   VSCE_TARGET=darwin-arm64 npm run package:platform     (or: node scripts/package-platform.mjs win32-x64)
// Through vsce's API rather than its command line, so the target is passed the same way on every OS
// (npm runs scripts with cmd.exe on Windows, which does not expand $VSCE_TARGET).
//
// vsce scans every packaged file outside node_modules for secrets. Here that includes the frozen core:
// the Python runtime and libraries, with our own code compressed inside, so the scan finds nothing real
// and its scanner failed on that bundle in CI. Our own files are scanned by `npm run package` (no core),
// which CI runs on every OS.

import { existsSync, statSync } from "node:fs";
import { createRequire } from "node:module";
import { join, resolve } from "node:path";

const here = resolve(import.meta.dirname, "..");
const target = process.argv[2] ?? process.env.VSCE_TARGET;
if (!target) {
  console.error("which target? e.g. VSCE_TARGET=darwin-arm64 (win32-x64, linux-x64, ...)");
  process.exit(2);
}
const exe = target.startsWith("win32") ? "benchscout.exe" : "benchscout";
if (!existsSync(join(here, "bin", "benchscout", exe))) {
  console.error(`no frozen core at bin/benchscout/${exe}: run \`uv run python packaging/build_core.py\` first`);
  process.exit(2);
}
const notices = join(here, "THIRD_PARTY_NOTICES.txt");
if (!existsSync(notices) || statSync(notices).mtimeMs < statSync(join(here, "bin", "benchscout", exe)).mtimeMs) {
  console.error("THIRD_PARTY_NOTICES.txt is missing or older than the core: run `uv run --with zstandard python packaging/notices.py`");
  process.exit(2);
}
process.chdir(here);
const { createVSIX } = createRequire(import.meta.url)("@vscode/vsce");
await createVSIX({
  target,
  packagePath: join(here, `benchscout-${target}.vsix`),
  skipLicense: true,
  allowMissingRepository: true,
  dependencies: false, // the extension is one bundled file (npm run bundle)
  allowPackageAllSecrets: true,
  allowPackageEnvFile: true,
});
