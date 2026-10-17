// SPDX-License-Identifier: AGPL-3.0-only
// Offline witness: systemd invocations are stubbed, never executed.
const { parentPort, workerData, Worker } = require('node:worker_threads');
if (workerData.nested) {
  const NestedWorker = globalThis.retainedWorker || Worker;
  const nested = new NestedWorker(workerData.workerFile, {
    execArgv: [], workerData: { ...workerData, nested: false }
  });
  nested.on('message', message => parentPort.postMessage(message));
  nested.on('error', error => { throw error; });
  nested.on('exit', code => parentPort.postMessage({ nestedExit: code }));
} else {
  const cp = require('node:child_process');
  cp.spawnSync = (command, args) => {
    parentPort.postMessage({ nativeProbe: command });
    return { status: 0, stdout: '', stderr: '' };
  };
  const { createRequire, syncBuiltinESMExports } = require('node:module');
  const { pathToFileURL } = require('node:url');
  const path = require('node:path');
  syncBuiltinESMExports();
  (async () => {
    const { LocalSubprocessRuntime } = await import(pathToFileURL(workerData.providerEntry));
    const req = createRequire(path.join(workerData.providerRoot, 'package.json'));
    const { Context } = await import(req.resolve('@deepseek-ai/cordis'));
    const ctx = new Context();
    ctx.logger = { warn: message => console.error(message) };
    const runtime = new LocalSubprocessRuntime(ctx);
    parentPort.postMessage({ mode: runtime.selectContainmentMode('ordinary') });
    await runtime.disposeManagedProcesses();
  })().catch(error => { throw error; });
}
