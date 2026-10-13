"""Targeted, database-free counterexample on the frozen #901 code."""
import json
from types import SimpleNamespace
from unittest.mock import patch

from ameesh import ci

old_event = {
    "v": 1, "type": "test", "execution": 801, "part": "1",
    "test": "tests.old_attempt_failure", "statut": "echec",
    "ts": "2026-10-13T14:00:00Z",
}
# Attempt 2 is queued, so its publisher has not yet replaced attempt 1's
# custom check run. The commit's latest check of this name still belongs to 1.
checks = {"check_runs": [{
    "name": "tests (flux) 1", "status": "completed", "conclusion": "failure",
    "output": {"text": json.dumps(old_event)},
}]}
inserted = []


def add_failure(repo, run_id, **failure):
    inserted.append(failure)
    return True


store = SimpleNamespace(ci=SimpleNamespace(
    base_failures=lambda *args: {}, add_failure=add_failure,
))
watcher = ci.Watcher(SimpleNamespace(), api=lambda path: checks)
row = {"run_id": 801, "head_sha": "a" * 40, "run_attempt": 2, "base": "main"}
with patch.object(ci.storage, "of", return_value=store):
    result = watcher._failures(None, "manaty/ameesh", row)

assert inserted[0]["run_attempt"] == 2
assert inserted[0]["test"] == old_event["test"]
assert result[0]["action"] == "failures"
print("REPRODUCED: attempt 1 failure inserted as attempt 2 while rerun is queued")
