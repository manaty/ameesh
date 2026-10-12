// SPDX-License-Identifier: AGPL-3.0-only
// Real installed provider, offline: no dsh session and no model request.
import { createRequire } from 'node:module';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { finished } from 'node:stream/promises';
const require = createRequire(path.join(process.argv[2], 'package.json'));
const { Context } = await import(require.resolve('@deepseek-ai/cordis'));
let runtime;
let booted;
const mode = process.argv[3].replace(/^boot-/, '');
if (process.argv[3].startsWith('boot-')) {
  // The actual dsh loader uses Node's internal importer with the profile base URL.
  const { boot } = await import(require.resolve('@deepseek-ai/dsh-app-boot'));
  booted = await boot('l490-test', path.join(path.dirname(fileURLToPath(import.meta.url)), 'provider.yml'));
  runtime = booted.subprocess;
} else {
  const { LocalSubprocessRuntime } = await import('@deepseek-ai/dsh-subprocess-local');
  const ctx = new Context();
  ctx.logger = { warn: message => console.error(message) };
  runtime = new LocalSubprocessRuntime(ctx);
}
const parent = readFileSync('/proc/self/cgroup', 'utf8').trim();
const shellQuote = value => "'" + value.replaceAll("'", "'\"'\"'") + "'";
const nodeChild = 'console.log("NODE-OPTIONS:" + (process.env.NODE_OPTIONS || "")); console.log(require("node:fs").readFileSync("/proc/self/cgroup", "utf8").trim());';
const command = 'cat /proc/self/cgroup; sh -c "cat /proc/self/cgroup"; setsid --wait sh -c "cat /proc/self/cgroup"; ' +
  shellQuote(process.execPath) + ' -e ' + shellQuote(nodeChild);
const report = output => {
  const lines = output.trim().split(/\r?\n/);
  return { parent, groups: lines.filter(line => line.startsWith('0::')),
    childNodeOptions: lines.find(line => line.startsWith('NODE-OPTIONS:'))?.slice('NODE-OPTIONS:'.length),
    nodeOptions: process.env.NODE_OPTIONS };
};
try {
  if (mode === 'terminal') {
    const handle = await runtime.spawnTerminal({ argv: ['/bin/sh', '-c', command],
      cwd: process.cwd(), rows: 24, cols: 80, graceMs: 200 });
    let output = '';
    handle.output.on('data', chunk => { output += chunk.toString(); });
    const drained = finished(handle.output, { readable: true, writable: false });
    const outcome = await handle.done;
    await drained;
    await handle.terminate();
    if (outcome.exitCode !== 0) throw new Error(JSON.stringify(outcome));
    console.log(JSON.stringify(report(output)));
  } else {
    const terminate = mode === 'terminate';
    const handle = runtime.spawn({ argv: terminate ? ['/bin/sleep', '30'] : ['/bin/sh', '-c', command],
      cwd: process.cwd(), stdio: { stdin: 'ignore', stdout: { maxBytes: 4096 }, stderr: { maxBytes: 4096 } }, graceMs: 200 });
    if (terminate) handle.terminate();
    const outcome = await handle.done;
    await handle.waitForExit();
    if (!terminate && outcome.exitCode !== 0) throw new Error(JSON.stringify(outcome));
    console.log(JSON.stringify({ ...report(handle.collected.stdout.readFrom(0).text), outcome }));
  }
} finally {
  await runtime.disposeManagedProcesses();
  if (booted) await booted.fiber.dispose();
}
