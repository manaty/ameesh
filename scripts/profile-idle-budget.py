# SPDX-License-Identifier: AGPL-3.0-only
"""Offline reproduction of the idle worker's real budget_reason path.
No database connection, real account or production transcript is used.
"""
import argparse
import cProfile
import io
import json
import os
import pstats
import tempfile
import time
from types import SimpleNamespace
from unittest.mock import patch
from ameesh import cost, storage
from ameesh.budget import Limits
from ameesh.runner import AgentWorker

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--output-dir', required=True, help='Directory for profile and text report')
parser.add_argument('--label', default='idle-budget')
args = parser.parse_args()
os.makedirs(args.output_dir, exist_ok=True)

with tempfile.TemporaryDirectory(prefix='ameesh-test-l244-') as root:
    state = os.path.join(root, 'state')
    profiles = []
    for name in ('primary', 'secondary', 'tertiary'):
        home = os.path.join(root, name)
        os.mkdir(home, 0o700)
        with open(os.path.join(home, '.credentials.json'), 'w'):
            pass
        os.chmod(os.path.join(home, '.credentials.json'), 0o600)
        profiles.append({'name': name, 'type': 'config_dir', 'path': home})
    tools = {f'agent-{i}': 'claude' for i in range(10)}
    filler = json.dumps({'type': 'assistant', 'text': 'x' * 1950}) + '\n'
    for agent in tools:
        path = os.path.join(state, agent)
        os.makedirs(path)
        with open(os.path.join(path, 'events.jsonl'), 'w') as stream:
            stream.write(json.dumps({'type': cost.ACCOUNT_MARKER,
                                    'account': 'primary', 'harness': 'claude',
                                    'ts': time.time()}) + '\n')
            stream.write(filler * 1900)
            stream.write(json.dumps({'type': 'rate_limit_event', 'rate_limit_info': {
                'unifiedWindows': {'five_hour': {'utilization': 99,
                                                  'resetsAt': time.time() + 9000}}}}) + '\n')
    cfg = SimpleNamespace(state_dir=state, host='offline-test',
                          accounts={'claude': profiles})
    limits = Limits(10, 'test', 0, 'test')
    worker = SimpleNamespace(name='agent-0', cfg=cfg,
        agent={'harness': 'claude'}, db=SimpleNamespace(schema='ameesh_test_l244'),
        runner=SimpleNamespace(budget_limits=lambda db: limits),
        current_session=lambda: None)
    fake_store = SimpleNamespace(accounts=SimpleNamespace(
        active=lambda *args: {'account': 'primary', 'forced': True}))
    # Only database operations are replaced. Real account selection, guard,
    # JSONL reading and JSON parsing run unchanged.
    with patch.object(storage, 'of', return_value=fake_store), \
         patch.object(cost.CostBook, 'registry_tools', return_value=tools), \
         patch.object(cost.CostBook, 'record_gauges', return_value=0):
        def run(count):
            for _ in range(count):
                assert 'forcé' in AgentWorker.budget_reason(worker)
        run(1)
        start = time.process_time()
        run(12)
        cpu = time.process_time() - start
        result = {'calls': 12, 'cpu_seconds': cpu, 'cpu_ms_per_poll': cpu / 12 * 1000,
                  'projected_core_percent_at_5s_poll': cpu / 12 / 5 * 100,
                  'fixture_agents': len(tools), 'fixture_bytes':
                  sum(os.path.getsize(os.path.join(state, a, 'events.jsonl')) for a in tools),
                  'scope': 'offline budget guard only; excludes DB, canon and host probes'}
        profiler = cProfile.Profile()
        profiler.runcall(run, 6)
        profiler.dump_stats(os.path.join(args.output_dir, args.label + '.prof'))
        output = io.StringIO()
        pstats.Stats(profiler, stream=output).strip_dirs().sort_stats('cumulative').print_stats(18)
        print(json.dumps(result, indent=2))
        print(output.getvalue())
        with open(os.path.join(args.output_dir, args.label + '.txt'), 'w') as report:
            report.write(json.dumps(result, indent=2) + '\n' + output.getvalue())
