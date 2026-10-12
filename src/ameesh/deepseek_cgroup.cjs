// SPDX-License-Identifier: AGPL-3.0-only
// Temporary adapter for dsh 0.2.0-rc.2 / subprocess-local 0.1.5-rc.2.
// Check the actual imported provider, including profile-local overrides, then
// modify only that module's bindings. No global child_process interception.
'use strict';
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const { fileURLToPath } = require('node:url');
const { createRequire, registerHooks } = require('node:module');

const PROVIDER = '@deepseek-ai/dsh-subprocess-local';
const VERSION = '0.1.5-rc.2';
const INDEX_HASH = 'a3f85e92ce5eddc824348f83cf685d3e417a26c5f1ab602a81fe1bd13315fe2c';
// Include the provider's imported helper code, not only its declared main.
const HASHES = new Set([INDEX_HASH,
  '674f0acce6cf7969a5cc9a5c188c7a8d76ff766434634b908d9a4b8a659b1c1a',
  '16719698906a0508aa7163e8a200dd5eb023ba59fe64f451a2b570936e44d47b']);
const CP_IMPORT = 'import { execFile, spawn, spawnSync } from "node:child_process";';

function fail(error) {
  console.error(`[ameesh] WARNING: DeepSeek cgroup protection unavailable: ${error.message}`);
  // Existing headless runner host_not_ready classification (#106).
  process.exit(127);
}

function packageOf(filename) {
  let directory = path.dirname(filename);
  for (;;) {
    const candidate = path.join(directory, 'package.json');
    if (fs.existsSync(candidate)) {
      return { file: candidate, pkg: JSON.parse(fs.readFileSync(candidate, 'utf8')) };
    }
    const parent = path.dirname(directory);
    if (parent === directory) return null;
    directory = parent;
  }
}

function verifySource(info, source) {
  const hash = crypto.createHash('sha256').update(source).digest('hex');
  if (info.pkg.name !== PROVIDER || info.pkg.version !== VERSION || !HASHES.has(hash)) {
    throw new Error(`unreviewed ${PROVIDER} ${info.pkg.version} (${hash}) at ${info.file}; review the cgroup adapter before running tools`);
  }
  return hash;
}

function scopedBindings() {
  // These names live ONLY in the verified provider module. Its other imports,
  // and every other module's child_process calls, keep Node's original bindings.
  return `import { execFile, spawn as ameeshOriginalSpawn, spawnSync as ameeshOriginalSync } from "node:child_process";
function ameeshDshScope(command, args) {
  return command === "systemd-run" && Array.isArray(args) &&
    args.includes("--user") && args.includes("--scope") &&
    args.some(arg => /^--unit=dsh-subprocess-(?:probe-)?\\d+-[a-f0-9]+$/.test(arg));
}
const spawnSync = (command, args, ...rest) => {
  if (ameeshDshScope(command, args)) {
    const error = new Error("ameesh: dsh scopes outside the runner cgroup are disabled");
    error.code = "ENOTSUP";
    return { error, status: null, signal: null, pid: 0,
      output: [null, null, null], stdout: null, stderr: null };
  }
  return ameeshOriginalSync(command, args, ...rest);
};
const spawn = (command, args, ...rest) => {
  if (ameeshDshScope(command, args)) {
    throw new Error("ameesh: unexpected dsh native scope launch; cgroup adapter requires review");
  }
  return ameeshOriginalSpawn(command, args, ...rest);
};`;
}

function installLoader() {
  if (typeof registerHooks !== 'function') {
    throw new Error('Node synchronous module hooks are unavailable (requires Node >=22.15)');
  }
  const entryURLs = new Set();
  return registerHooks({
    resolve(specifier, context, nextResolve) {
      const resolved = nextResolve(specifier, context);
      // A bare package export must resolve to a provider-owned file; do not
      // silently accept an export/symlink that moves the code outside it.
      if (specifier === PROVIDER || specifier.startsWith(PROVIDER + '/')) {
        try {
          const info = resolved.url.startsWith('file:') ? packageOf(fileURLToPath(resolved.url)) : null;
          if (!info || info.pkg.name !== PROVIDER) throw new Error('provider export resolves outside its package');
          if (specifier === PROVIDER) entryURLs.add(resolved.url);
        } catch (error) { fail(error); }
      }
      return resolved;
    },
    load(url, context, nextLoad) {
      const loaded = nextLoad(url, context);
      if (!url.startsWith('file:')) return loaded;
      try {
        const info = packageOf(fileURLToPath(url));
        if (!info) return loaded;
        const providerPath = path.basename(path.dirname(info.file)) === 'dsh-subprocess-local' &&
          path.basename(path.dirname(path.dirname(info.file))) === '@deepseek-ai';
        if (info.pkg.name !== PROVIDER && !providerPath) return loaded;
        if (loaded.format !== 'module') throw new Error('unexpected provider module format');
        if (loaded.source === null || loaded.source === undefined) throw new Error('provider source is unavailable for verification');
        const source = typeof loaded.source === 'string' ? loaded.source : Buffer.from(loaded.source).toString('utf8');
        const hash = verifySource(info, source);
        if (entryURLs.has(url) && hash !== INDEX_HASH) throw new Error('provider export does not select the reviewed runtime entry');
        if (hash !== INDEX_HASH) return loaded;
        if (!source.includes(CP_IMPORT)) throw new Error('reviewed provider bindings are missing');
        return { ...loaded, source: source.replace(CP_IMPORT, scopedBindings()) };
      } catch (error) { fail(error); }
    }
  });
}

function activate() {
  // Early check uses Node's entry resolution (exports, not pkg.main). Actual
  // imports are independently checked by the loader, wherever the profile
  // resolves them. This check is not used as permission to load an override.
  let directory = path.dirname(fs.realpathSync(process.argv[1]));
  let dshPackage;
  for (;;) {
    const candidate = path.join(directory, 'package.json');
    if (fs.existsSync(candidate) && JSON.parse(fs.readFileSync(candidate, 'utf8')).name === '@deepseek-ai/dsh') {
      dshPackage = candidate;
      break;
    }
    const parent = path.dirname(directory);
    if (parent === directory) throw new Error('cannot locate the dsh package/provider to verify cgroup compatibility');
    directory = parent;
  }
  const entry = createRequire(dshPackage).resolve(PROVIDER);
  const info = packageOf(entry);
  if (!info) throw new Error('cannot locate resolved provider metadata');
  // CJS resolution is an early availability check. The load hook checks the
  // actual ESM/import condition's source before evaluating it as well.
  if (verifySource(info, fs.readFileSync(entry)) !== INDEX_HASH) {
    throw new Error('CLI provider export does not select the reviewed runtime entry');
  }
  installLoader();
  // Preserve other options for Node tools without propagating this dsh policy.
  const option = '--require=' + JSON.stringify(__filename);
  process.env.NODE_OPTIONS = (process.env.NODE_OPTIONS || '').replace(option, '').trim();
}

module.exports = { verifySource, installLoader, scopedBindings };
if (process.platform === 'linux' && (process.env.NODE_OPTIONS || '').includes('--require=' + JSON.stringify(__filename))) {
  try { activate(); } catch (error) { fail(error); }
}
