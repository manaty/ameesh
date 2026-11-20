import os, sys, time, unittest
sys.path.insert(0, os.getcwd())
from tests.support import PgTestCase

orig_wait = PgTestCase.wait_for
def wait_for(self, predicate, timeout=10.0, interval=0.1):
    t0 = time.monotonic()
    try:
        r = orig_wait(self, predicate, timeout, interval)
        print("[instr] wait_for OK after %.2fs (timeout %.0fs)" % (time.monotonic()-t0, timeout), flush=True)
        return r
    except Exception as e:
        print("[instr] wait_for FAIL after %.2fs: %s" % (time.monotonic()-t0, e), flush=True)
        raise
PgTestCase.wait_for = wait_for

t0 = time.monotonic()
suite = unittest.defaultTestLoader.loadTestsFromName(
    "tests.test_runner.RunnerTest.test_branche_bail_perdu_ne_perd_pas_la_consigne")
res = unittest.TextTestRunner(verbosity=2).run(suite)
print("[instr] total %.2fs ok=%s" % (time.monotonic()-t0, res.wasSuccessful()), flush=True)
