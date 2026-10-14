import json, os, pathlib, subprocess, tempfile, time, unittest.mock as mock
ROOT = pathlib.Path(__file__).resolve().parents[4]
os.chdir(ROOT)
import sys
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))
from tests import test_1256_ci_runners as tests
script = (ROOT / 'deploy/guest/ci-runners.sh').read_text()
functions = script[script.index('runner_stop_delay_s()'):script.index('slice_state()')]
real_popen = subprocess.Popen
with tempfile.TemporaryDirectory() as d:
    host = pathlib.Path(d)
    gate = host / 'state.json'
    prelude = 'set -euo pipefail\nHOSTDIR=' + repr(d) + '\nHERE=' + repr(str(ROOT / 'deploy/guest')) + '\nGATE_STATE=/state.json\nat() { printf "%s%s" "$HOSTDIR" "$1"; }\nsay() { :; }\n'
    def bash(tail, prefix=''):
        result = subprocess.run(['bash', '-c', prelude + prefix + functions + tail], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
        assert 'unbound variable' not in result.stderr, result.stderr
        return result.stdout.strip()
    assert bash('runner_stop_delay_s') == '0'
    assert bash('runner_stop_delay_s', 'CI_RUNNERS_TEST_STOP_DELAY=invalid\n') == '0'
    assert bash('runner_stop_delay_s', 'CI_RUNNERS_TEST_STOP_DELAY=10\n') == '10'
    assert bash('runner_stop_delay_s', 'HOSTDIR=""\nunset CI_RUNNERS_TEST_STOP_DELAY\n') == '157'
    print('C3: absent/invalid/valid seams and real default 157: OK; stderr clean')
    for state, mode, ttl, expected in [('available','enforce',30,'yes'), ('available','observe',30,'no'), ('available','enforce',-1,'no'), ('stopped','enforce',30,'no')]:
        gate.write_text(json.dumps(dict(schema='ameesh-host-state/1', state=state, seq=1, expires_ts=time.time()+ttl, guard=dict(mode=mode))))
        assert bash('gate_obeys') == expected
    print('C2: fresh/enforce only; observe/stale/stopped closed: OK')
    tail = '\nrunner_child() { return 0; }\nrunner_stop_delay_s() { echo 0; }\ngate_obeys() { echo yes; }\nrc=0; wait_runner_stops 1 || rc=$?; echo "$rc"\n'
    assert bash(tail) == '2'
    tail = tail.replace('echo yes;', 'echo no;')
    assert bash(tail) == '1'
    assert bash(tail.replace('runner_child() { return 0;', 'runner_child() { return 1;')) == '0'
    print('C2: wait statuses reopened=2 / expired=1 / child gone=0: OK')

    # Deterministic scheduler perturbation: the script starts only after the
    # test's fixed sleep and state mutation. No wall-clock delay or host action.
    for name in ['test_check_accepts_a_runner_stopped_within_the_delay', 'test_check_reads_the_gate_again_and_accepts_a_reopening']:
        fifo = host / name
        os.mkfifo(fifo)
        def delayed_popen(argv, **kwargs):
            proc = real_popen(['bash', '-c', 'read -r release < "$1"; shift; exec "$@"', 'probe', str(fifo), *argv], **kwargs)
            communicate = proc.communicate
            def release(**kw):
                with fifo.open('w') as pipe:
                    pipe.write('go\n')
                return communicate(**kw)
            proc.communicate = release
            return proc
        case = tests.CiRunnersTest(name)
        case.setUp()
        try:
            with mock.patch.object(tests.subprocess, 'Popen', delayed_popen), mock.patch.object(tests.time, 'sleep', lambda _: None):
                try:
                    getattr(case, name)()
                except AssertionError as exc:
                    assert 'waiting up to' in str(exc), str(exc)
                    print(name + ': reproduced false failure: missing waiting message when scheduled after state mutation')
                else:
                    raise AssertionError('expected timing-dependent assertion to fail')
        finally:
            case.doCleanups()
