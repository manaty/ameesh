// SPDX-License-Identifier: AGPL-3.0-only
// Temporary adapter for dsh 0.2.0-rc.2 / subprocess-local 0.1.5-rc.2.
// Select dsh's existing PGID/PTY fallback BEFORE it can start a command.
// It inherits the runner cgroup, including setsid descendants. PGID cleanup
// cannot catch every escaped descendant; only runner-unit teardown does.
'use strict';
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const cp = require('node:child_process');
const { createRequire, syncBuiltinESMExports } = require('node:module');

const PROVIDER_VERSION = '0.1.5-rc.2';
const PROVIDER_SHA256 = 'a3f85e92ce5eddc824348f83cf685d3e417a26c5f1ab602a81fe1bd13315fe2c';

function verifyProvider(packageFile) {
  const pkg = JSON.parse(fs.readFileSync(packageFile, 'utf8'));
  const entry = path.resolve(path.dirname(packageFile), pkg.main);
  const hash = crypto.createHash('sha256').update(fs.readFileSync(entry)).digest('hex');
  if (pkg.name !== '@deepseek-ai/dsh-subprocess-local' ||
      pkg.version !== PROVIDER_VERSION || hash !== PROVIDER_SHA256) {
    throw new Error(`unreviewed dsh-subprocess-local ${pkg.version} (${hash}); review the cgroup adapter before running tools`);
  }
}

function isDshScope(command, args) {
  return path.basename(String(command)) === 'systemd-run' && Array.isArray(args) &&
    args.includes('--user') && args.includes('--scope') &&
    args.some(arg => /^--unit=dsh-subprocess-(?:probe-)?\d+-[a-f0-9]+$/.test(arg));
}

function installPolicy() {
  const originalSync = cp.spawnSync;
  const originalSpawn = cp.spawn;
  cp.spawnSync = function(command, args, ...rest) {
    if (isDshScope(command, args)) {
      const error = new Error('ameesh: dsh scopes outside the runner cgroup are disabled');
      error.code = 'ENOTSUP';
      // dsh checks error/status before choosing its existing fallback. Nothing
      // has executed, so there is no replay of a possibly started command.
      return { error, status: null, signal: null, pid: 0,
        output: [null, null, null], stdout: null, stderr: null };
    }
    return originalSync.call(this, command, args, ...rest);
  };
  cp.spawn = function(command, args, ...rest) {
    // Defense against an unexpected cached/skipped probe; never launch it.
    if (isDshScope(command, args)) {
      throw new Error('ameesh: unexpected dsh native scope launch; cgroup adapter requires review');
    }
    return originalSpawn.call(this, command, args, ...rest);
  };
  // dsh imports named ESM bindings; changing only the CommonJS object is insufficient.
  syncBuiltinESMExports();
}

function activate() {
  let directory = path.dirname(fs.realpathSync(process.argv[1]));
  let dshPackage;
  for (;;) {
    const candidate = path.join(directory, 'package.json');
    if (fs.existsSync(candidate) &&
        JSON.parse(fs.readFileSync(candidate, 'utf8')).name === '@deepseek-ai/dsh') {
      dshPackage = candidate;
      break;
    }
    const parent = path.dirname(directory);
    if (parent === directory) throw new Error('cannot locate the dsh package/provider to verify cgroup compatibility');
    directory = parent;
  }
  verifyProvider(createRequire(dshPackage).resolve('@deepseek-ai/dsh-subprocess-local/package.json'));
  installPolicy();
  // The policy belongs to this harness, not arbitrary Node tools it spawns.
  const option = '--require=' + JSON.stringify(__filename);
  process.env.NODE_OPTIONS = (process.env.NODE_OPTIONS || '').replace(option, '').trim();
}

module.exports = { verifyProvider, installPolicy, isDshScope };
// A regular require is a test seam. Production loads this through NODE_OPTIONS.
if (process.platform === 'linux' && (process.env.NODE_OPTIONS || '').includes('--require=' + JSON.stringify(__filename))) {
  try {
    activate();
  } catch (error) {
    // 127 with no stdout/session is classified as host_not_ready by the runner
    // (#106), producing its existing operational alert and preserving the turn.
    console.error(`[ameesh] WARNING: DeepSeek cgroup protection unavailable: ${error.message}`);
    process.exit(127);
  }
}
