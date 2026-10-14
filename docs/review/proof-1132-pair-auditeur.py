# SPDX-License-Identifier: AGPL-3.0-only
"""Validation en binôme #1132 : production égale, NULL, corrections R1.

Journaux factices et SELECT réels sur CTE VALUES, en transaction READ ONLY.
Aucune lecture ni écriture des tables de jauges, aucune suite auteur rejouée.
La réussite signifie que les témoins corrigés sont vérifiés sur cette tête.
"""
import ast
import json
import os
import subprocess
import tempfile
from pathlib import Path

from ameesh import accounts, config, cost, db
from ameesh.mediated_executor import contract as C
from ameesh.storage import interface as I
from ameesh.storage.postgres.operations import Operations

NOW = 1800000000.0
RESET = NOW + 3 * 86400


def marker(ts):
    return {"type": cost.ACCOUNT_MARKER, "harness": "claude",
            "account": "secondaire", "ts": ts}


def event(used, short_reset=NOW + 7200):
    return {"type": "rate_limit_event", "rate_limit_info": {"unifiedWindows": {
        "five_hour": {"utilization": .2, "resetsAt": short_reset},
        "seven_day": {"utilization": used, "resetsAt": RESET}}}}


with tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"], prefix="1132-delta-") as task_tmp:
    root = Path(task_tmp)
    journal = root / "state" / "same-agent" / "events.jsonl"
    journal.parent.mkdir(parents=True)
    profile_dir = root / "account"
    profile_dir.mkdir(mode=0o700)
    profiles = accounts.parse({"claude": [{"name": "secondaire", "path": str(profile_dir),
                                          "cap": .8, "check_credentials": False}]})["claude"]

    def book(lines):
        journal.write_text("".join(json.dumps(e) + "\n" for e in lines))
        return cost.CostBook(state_dir=str(root / "state"), prices_path=str(root / "prices.json"),
                             codex_sessions=str(root / "codex"), db=None,
                             clock=lambda: NOW, tools={"same-agent": "claude"})

    def admission(b):
        return accounts.evaluate(b, profiles[0], profiles, NOW, record=False, critical=True)

    fresh = [marker(NOW - 3600), event(.87)]
    replay = [marker(NOW - 5), event(.67, NOW - 90000)]
    b = book(fresh + replay)
    assert admission(b).ok is False
    named = {g.key: g for g in b.claude_gauges(account="secondaire", primary="primaire")}
    unnamed = {g.key: g for g in b.claude_gauges()}
    assert named["seven_day"].used == unnamed["seven_day"].used == .87
    assert named["seven_day"].produced_at == NOW - 3600
    assert admission(book(fresh + [marker(NOW - 1), event(.67)])).ok is True
    print("Correction 1 : rejeu postérieur rejeté, avec/sans compte ; baisse datée acceptée.")

    overage = event(1.0)
    overage["rate_limit_info"].update(overageStatus="allowed", isUsingOverage=True)
    gauges = book([marker(NOW - 10), overage]).claude_gauges()
    assert isinstance(gauges, cost.Gauges) and gauges.in_overage(NOW)
    print("Overage sans compte : drapeaux conservés.")

    # Multiple events under one turn marker are ordered by their appearance.
    same_turn = [marker(NOW - 3600), event(.87), event(.67), event(.87)]
    assert admission(book(same_turn)).ok is False
    assert {g.key: g for g in book(same_turn).claude_gauges()}["seven_day"].used == .87
    print("Journal, production égale : la dernière mesure 87 % est retenue.")

    class Capture:
        def query(self, sql, params):
            self.sql, self.params = sql, params
            return []

    def cte(history):
        values = []
        params = []
        for index, (used, produced, observed) in enumerate(history, 1):
            values.append("(%s::text,%s::text,%s::float8,to_timestamp(%s::float8),"
                          "%s::float8,%s::text,to_timestamp(%s::float8),%s::text,"
                          "to_timestamp(%s::float8),%s::bigint)")
            params.extend(("claude", "seven_day", used, RESET, 604800, "secondaire",
                           produced, "synthetic", observed, index))
        return ("WITH quota_gauge_readings(harness,gauge_key,used,resets_at,window_s,account,"
                "produced_at,source,observed_at,id) AS (VALUES " + ",".join(values) + ") ", params)

    baseline = subprocess.run(["git", "show", "84bec820:src/ameesh/storage/postgres/operations.py"],
                              check=True, capture_output=True, text=True).stdout
    old_class = next(n for n in ast.parse(baseline).body
                     if isinstance(n, ast.ClassDef) and n.name == "Operations")
    old_method = next(n for n in old_class.body
                      if isinstance(n, ast.FunctionDef) and n.name == "record_gauges")
    baseline_ns = {}
    exec(compile(ast.Module(body=[old_method], type_ignores=[]), "baseline_record_gauges", "exec"),
         baseline_ns)

    def candidates(connection, history, used, produced, method=None):
        capture = Capture()
        operations = Operations(capture)
        method = method or Operations.record_gauges
        method(operations, [{
            "harness": "claude", "account": "secondaire", "key": "seven_day",
            "used": used, "resets_at": RESET, "window_s": 604800,
            "produced_at": produced, "source": "synthetic-new"}])
        insert = capture.sql.index("INSERT INTO quota_gauge_readings")
        leading = capture.sql[:insert].strip()
        select = capture.sql[capture.sql.index("SELECT ", insert):]
        select = select[:select.index("RETURNING id")].replace("now()", f"to_timestamp({NOW})")
        prefix, params = cte(history)
        if leading:
            assert leading.startswith("WITH ")
            prefix = prefix.rstrip() + ", " + leading[5:] + " "
        return connection.query(prefix + select, tuple(params) + capture.params)

    def latest(connection, history):
        capture = Capture()
        Operations(capture).gauge_latest(harness="claude", account="secondaire")
        prefix, params = cte(history)
        return connection.query(prefix + capture.sql, tuple(params) + capture.params)

    connection = db.connect(config.load())
    try:
        with connection.transaction() as read_only:
            read_only.execute("SET TRANSACTION READ ONLY")
            read_only.execute("SET LOCAL statement_timeout = '5s'")
            assert read_only.query("SELECT current_setting('transaction_read_only') AS mode")[0]["mode"] == "on"
            history = [(.67, NOW - 3600, NOW - 2), (.87, NOW - 7200, NOW - 1)]
            assert len(candidates(read_only, history, .87, NOW - 30)) == 1
            assert len(candidates(read_only, history, .87, NOW - 7200)) == 0
            assert len(candidates(read_only, [(.67, None, NOW - 1)], .67, NOW - 60)) == 1
            accepted = history + [(.87, NOW - 30, NOW)]
            assert float(latest(read_only, accepted)[0]["used"]) == .87
            print("Correction 2 : production neuve conservée ; rejeu identique dédupliqué ; NULL→date accepté.")

            # Correction paired with the author: at equal marker timestamps,
            # the latest value at that production rank decides deduplication.
            tied = [(.87, NOW - 3600, NOW - 3), (.67, NOW - 3600, NOW - 2)]
            rows = candidates(read_only, tied, .87, NOW - 3600)
            old_rows = candidates(read_only, tied, .87, NOW - 3600,
                                  baseline_ns["record_gauges"])
            assert len(rows) == 1
            assert len(old_rows) == 1
            retained = latest(read_only, tied + [(.87, NOW - 3600, NOW - 1)])
            assert float(retained[0]["used"]) == .87
            remote = book([])
            remote.db = object()
            remote._gauge_latest = lambda *args: retained
            result = admission(remote)
            assert result.ok is False
            assert candidates(read_only, tied + [(.87, NOW - 3600, NOW - 1)],
                              .87, NOW - 3600) == []
            print(json.dumps({"case": "same production: 87->67->87",
                              "baseline_insert_rows": len(old_rows),
                              "insert_candidate_rows": len(rows), "history_used": .87,
                              "local_admission": False, "shared_history_admission": result.ok}))
            undated = [(.67, None, NOW - 3), (.87, None, NOW - 2)]
            assert len(candidates(read_only, undated, .67, None)) == 1
            assert len(candidates(read_only, undated, .67, None,
                                  baseline_ns["record_gauges"])) == 1
            assert float(latest(read_only, undated + [(.67, None, NOW - 1)])[0]["used"]) == .67
            assert candidates(read_only, undated + [(.67, None, NOW - 1)], .67, None) == []
            print("NULL : 67→87→67 conserve la dernière 67, répétition dédupliquée.")

            # Real now() can tie throughout a transaction. The id tie breaker
            # must still retain 87→67→87, not an older identical value.
            tied_clock = [(.87, NOW - 3600, NOW - 2), (.67, NOW - 3600, NOW - 2)]
            assert len(candidates(read_only, tied_clock, .87, NOW - 3600)) == 1
            same_clock_latest = tied_clock + [(.87, NOW - 3600, NOW - 2)]
            assert float(latest(read_only, same_clock_latest)[0]["used"]) == .87
            assert candidates(read_only, same_clock_latest, .87, NOW - 3600) == []
            print("Production et observation égales : id départage, dernière valeur conservée.")
    finally:
        connection.close()

contract = C.load()
operations = set()
for domain, field in I.Storage.__annotations__.items():
    cls = getattr(I, field, None) if isinstance(field, str) else field
    if hasattr(cls, "__abstractmethods__"):
        operations.update(domain + "." + name for name in cls.__abstractmethods__)
classified = set(contract.operations) | {d + "." + m for d, ms in contract.refused.items() for m in ms}
assert "operations.gauge_latest" in classified and "operations.gauge_latest" not in contract.operations
assert contract.interface_total == 386
try:
    contract.get("operations.gauge_latest")
except C.NotSupportedRemotely:
    pass
else:
    raise AssertionError("gauge_latest unexpectedly allowed remotely")
print(json.dumps({"correction": 3, "gauge_latest": "refused", "declared": contract.interface_total,
                  "actual": len(operations), "preexisting_missing": sorted(operations - classified)}))

pilot = Path("tests/parts/pilote-psql.txt").read_text().splitlines()
names = [line.split("#", 1)[0].strip() for line in pilot if line.split("#", 1)[0].strip()]
assert names.count("test_1132_jauges_perimees") == 1 and names == sorted(names)
print("Correction 4 : module PostgreSQL présent une fois et trié dans le pilote psql.")
