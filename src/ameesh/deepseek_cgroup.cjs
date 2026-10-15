// SPDX-License-Identifier: AGPL-3.0-only
// Temporary adapter for dsh 0.2.0-rc.2 / subprocess-local 0.1.5-rc.2.
// Check the actual imported provider, including profile-local overrides, then
// modify only that module's bindings. No global child_process interception.
'use strict';
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const { fileURLToPath } = require('node:url');
const nodeModule = require('node:module');
const { createRequire, registerHooks, syncBuiltinESMExports } = nodeModule;
const threads = require('node:worker_threads');

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

function providerFile(filename) {
  const info = packageOf(filename);
  if (!info) return false;
  return info.pkg.name === PROVIDER ||
    (path.basename(path.dirname(info.file)) === 'dsh-subprocess-local' &&
     path.basename(path.dirname(path.dirname(info.file))) === '@deepseek-ai');
}

function optionTokens(text) {
  // NODE_OPTIONS uses double quotes and escapes, not shell expansion.
  const tokens = [];
  let token = '', quoted = false, started = false;
  for (let i = 0; i < text.length; i++) {
    const char = text[i];
    if (char === '"') { quoted = !quoted; started = true; }
    else if (char === '\\' && quoted) {
      if (++i === text.length) throw new Error('invalid escape in NODE_OPTIONS');
      token += text[i]; started = true;
    } else if (/\s/.test(char) && !quoted) {
      if (started) tokens.push(token);
      token = ''; started = false;
    } else { token += char; started = true; }
  }
  if (quoted) throw new Error('unclosed quote in NODE_OPTIONS');
  if (started) tokens.push(token);
  return tokens;
}

function preloadTargets(tokens, includeImports = true) {
  const targets = [];
  for (let i = 0; i < tokens.length; i++) {
    const flag = tokens[i];
    if (flag === '--require' || flag === '--import' || flag === '-r') {
      if (!tokens[++i]) throw new Error('missing Node preload target');
      if (includeImports || flag !== '--import') targets.push(tokens[i]);
    } else if (flag.startsWith('--require=') || flag.startsWith('--import=')) {
      if (includeImports || !flag.startsWith('--import=')) targets.push(flag.slice(flag.indexOf('=') + 1));
    } else if (flag.startsWith('-r') && flag.length > 2) {
      targets.push(flag.slice(2));
    }
  }
  return targets;
}

function resolvePreload(target) {
  if (target.startsWith('file:')) return fs.realpathSync(fileURLToPath(target));
  return createRequire(path.join(process.cwd(), '__ameesh_preloads__.cjs')).resolve(target);
}

function assertSupportedOptions(nodeOptions, argv) {
  for (const token of [...optionTokens(nodeOptions), ...argv]) {
    const flag = token.split('=', 1)[0];
    if (['--loader', '--experimental-loader', '--experimental-vm-modules'].includes(flag)) {
      throw new Error(`unsupported Node option for the cgroup guard: ${flag}`);
    }
  }
}

function assertNoProviderPreloads(nodeOptions = process.env.NODE_OPTIONS || '', argv = process.execArgv, checkCache = true) {
  assertSupportedOptions(nodeOptions, argv);
  if (checkCache) for (const filename of Object.keys(require.cache)) {
    if (providerFile(filename)) throw new Error(`provider already loaded before the cgroup guard: ${filename}`);
  }
  for (const target of preloadTargets([...optionTokens(nodeOptions), ...argv])) {
    // Bare names and unresolved profile paths also fail closed; no cached
    // provider is repaired or granted permission after its evaluation.
    if (target === PROVIDER || target.startsWith(PROVIDER + '/') ||
        target.includes('/@deepseek-ai/dsh-subprocess-local/')) {
      throw new Error(`provider declared as a Node preload: ${target}`);
    }
    let filename;
    try { filename = resolvePreload(target); } catch { continue; }
    if (providerFile(filename)) throw new Error(`provider declared as a Node preload: ${target}`);
  }
}

function assertGuardFirst() {
  const tokens = [...optionTokens(process.env.NODE_OPTIONS || ''), ...process.execArgv];
  const targets = preloadTargets(tokens, false);
  const own = targets.findIndex(target => {
    try { return resolvePreload(target) === __filename; } catch { return false; }
  });
  if (own < 0 && preloadTargets(tokens).some(target => {
    try { return resolvePreload(target) === __filename; } catch { return false; }
  })) throw new Error('cgroup guard must be loaded with --require, before ESM --import preloads');
  if (own > 0) throw new Error('cgroup guard must be the first --require preload; earlier modules can retain unguarded Worker references');
}

function protectWorkers() {
  const OriginalWorker = threads.Worker;
  threads.Worker = class CgroupGuardedWorker extends OriginalWorker {
    constructor(filename, options = {}) {
      const argv = options.execArgv === undefined ? process.execArgv : options.execArgv;
      const env = options.env && options.env !== threads.SHARE_ENV ? options.env : process.env;
      const nodeOptions = env.NODE_OPTIONS || '';
      try {
        assertNoProviderPreloads(nodeOptions, argv, false);
        // NODE_OPTIONS requires run before execArgv requires. With a private
        // env put the guard first there too. SHARE_ENV cannot be changed just
        // for one worker, so refuse ambient require preloads in that case.
        if (options.env === threads.SHARE_ENV && preloadTargets(optionTokens(nodeOptions), false).length) {
          throw new Error('SHARE_ENV workers with ambient --require preloads are unsupported by the cgroup guard');
        }
      }
      catch (error) { fail(error); }
      // Explicit execArgv: [] cannot discard the thread-local guard. Install
      // it first; every guarded worker also wraps nested Worker creation.
      const guardedEnv = options.env === threads.SHARE_ENV ? threads.SHARE_ENV :
        { ...env, NODE_OPTIONS: '--require=' + JSON.stringify(__filename) + ' ' + nodeOptions };
      super(filename, { ...options, env: guardedEnv, execArgv: ['--require', __filename, ...argv] });
    }
  };
  // Update named ESM Worker imports; child_process bindings remain untouched.
  syncBuiltinESMExports();
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

function installLoader(rejectProvider = false) {
  assertNoProviderPreloads();
  assertGuardFirst();
  if (typeof registerHooks !== 'function') {
    throw new Error('Node synchronous module hooks are unavailable (requires Node >=22.15)');
  }
  // Asynchronous loaders run in a separate internal thread that is outside
  // the public Worker wrapper and cannot inherit our synchronous hooks.
  nodeModule.register = function () {
    throw new Error('module.register is unsupported by the cgroup guard');
  };
  syncBuiltinESMExports();
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
        if (!providerFile(fileURLToPath(url))) return loaded;
        if (rejectProvider) throw new Error('dsh provider is not allowed in a worker thread');
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
  assertNoProviderPreloads();
  assertGuardFirst();
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
  protectWorkers();
  // Preserve other options for Node tools without propagating this dsh policy.
  const option = '--require=' + JSON.stringify(__filename);
  process.env.NODE_OPTIONS = (process.env.NODE_OPTIONS || '').replace(option, '').trim();
}

module.exports = { verifySource, installLoader, scopedBindings };
if (process.platform === 'linux') {
  try {
    const targets = preloadTargets([...optionTokens(process.env.NODE_OPTIONS || ''), ...process.execArgv]);
    const ownPreload = targets.some(target => {
      try { return resolvePreload(target) === __filename; } catch { return false; }
    });
    if (ownPreload) {
      if (threads.isMainThread) activate();
      else {
        installLoader(true);
        protectWorkers();
        const option = '--require=' + JSON.stringify(__filename);
        process.env.NODE_OPTIONS = (process.env.NODE_OPTIONS || '').replace(option, '').trim();
      }
    }
  } catch (error) { fail(error); }
}
