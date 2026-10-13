"""B1 closure witness: provenance through publisher and watcher, no database."""
import json
import runpy
from types import SimpleNamespace
from unittest.mock import patch

from ameesh import ci

flow = runpy.run_path("scripts/ci-flow.py")
github = flow["GitHub"](env={"GITHUB_RUN_ATTEMPT": "2"})
assert github.attempt == 2
old = {
    "v": 1, "type": "test", "execution": 801, "part": "1",
    "test": "tests.old_attempt_failure", "statut": "echec",
    "ts": "2026-10-13T14:00:00Z",
}
inserted = []


def add_failure(repo, run_id, **failure):
    inserted.append(failure)
    return True


store = SimpleNamespace(ci=SimpleNamespace(
    base_failures=lambda *args: {}, add_failure=add_failure,
))
checks = {"check_runs": [{
    "name": "tests (flux) 1", "status": "completed", "conclusion": "failure",
    "output": {"text": json.dumps(old)},
}]}
watcher = ci.Watcher(SimpleNamespace(), api=lambda path: checks)
row = {"run_id": 801, "head_sha": "a" * 40, "run_attempt": 2, "base": "main"}
with patch.object(ci.storage, "of", return_value=store):
    assert watcher._failures(None, "manaty/ameesh", row) == []
    assert inserted[0]["run_attempt"] == 1
    # Same-name checks from both attempts must preserve provenance and not
    # hide the new failure. Publisher marks the new event with attempt 2.
    current = dict(old, test="tests.current_attempt_failure")
    checks["check_runs"].append({
        "name": "tests (flux) 1",
        "output": flow["check_output"]([current], github.attempt),
    })
    result = watcher._failures(None, "manaty/ameesh", row)
    assert result[0]["tests"] == [current["test"]]
    assert inserted[-1]["run_attempt"] == 2
    assert ci.events_of(checks["check_runs"], 801, 2)[0]["test"] == current["test"]
print("PASS: queued rerun ignores old failure; current publisher/watcher provenance = 2")
