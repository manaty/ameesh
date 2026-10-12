// SPDX-License-Identifier: AGPL-3.0-only
// Real installed provider, offline: no dsh session and no model request.
import { createRequire } from 'node:module';
import { readFileSync } from 'node:fs';
import path from 'node:path';
const require = createRequire(path.join(process.argv[2], 'package.json'));
const { LocalSubprocessRuntime } = await import(require.resolve('@deepseek-ai/dsh-subprocess-local'));
const { Context } = await import(require.resolve('@deepseek-ai/cordis'));
const ctx = new Context();
ctx.logger = { warn: message => console.error(message) };
const runtime = new LocalSubprocessRuntime(ctx);
const parent = readFileSync('/proc/self/cgroup', 'utf8').trim();
const command = 'cat /proc/self/cgroup; sh -c "cat /proc/self/cgroup"; setsid sh -c "cat /proc/self/cgroup"';
try {
  if (process.argv[3] === 'terminal') {
    const handle = await runtime.spawnTerminal({ argv: ['/bin/sh', '-c', command],
      cwd: process.cwd(), rows: 24, cols: 80, graceMs: 200 });
    let output = '';
    handle.output.on('data', chunk => { output += chunk.toString(); });
    const outcome = await handle.done;
    await handle.terminate();
    if (outcome.exitCode !== 0) throw new Error(JSON.stringify(outcome));
    console.log(JSON.stringify({ parent, groups: output.trim().split(/\r?\n/), nodeOptions: process.env.NODE_OPTIONS }));
  } else {
    const terminate = process.argv[3] === 'terminate';
    const handle = runtime.spawn({ argv: terminate ? ['/bin/sleep', '30'] : ['/bin/sh', '-c', command],
      cwd: process.cwd(), stdio: { stdin: 'ignore', stdout: { maxBytes: 4096 }, stderr: { maxBytes: 4096 } }, graceMs: 200 });
    if (terminate) handle.terminate();
    const outcome = await handle.done;
    await handle.waitForExit();
    if (!terminate && outcome.exitCode !== 0) throw new Error(JSON.stringify(outcome));
    console.log(JSON.stringify({ parent, groups: handle.collected.stdout.readFrom(0).text.trim().split(/\r?\n/),
      outcome, nodeOptions: process.env.NODE_OPTIONS }));
  }
} finally {
  await runtime.disposeManagedProcesses();
}
