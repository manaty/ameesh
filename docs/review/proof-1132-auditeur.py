# SPDX-License-Identifier: AGPL-3.0-only
import inspect, json, os, tempfile
from pathlib import Path
from ameesh import cost, accounts
from ameesh.mediated_executor import contract as C
from ameesh.storage import interface as I
from ameesh.storage.postgres.operations import Operations

NOW=1800000000.0
RESET=NOW+3*86400

def marker(ts):
    return {"type":cost.ACCOUNT_MARKER,"harness":"claude","account":"secondaire","ts":ts}

def event(used, short_reset):
    return {"type":"rate_limit_event","rate_limit_info":{"unifiedWindows":{
        "five_hour":{"utilization":.2,"resetsAt":short_reset},
        "seven_day":{"utilization":used,"resetsAt":RESET}}}}

# P2.1: the new reader discards an earlier dated fresh measurement in the
# same journal before ranking that journal against the others.
with tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"],prefix="1132-state-") as task_tmp:
    root=Path(task_tmp)
    agent=root/"state"/"same-agent"
    agent.mkdir(parents=True)
    profile_dir=root/"account"
    profile_dir.mkdir(mode=0o700)
    events=agent/"events.jsonl"
    fresh=[marker(NOW-3600),event(.87,NOW+7200)]
    replay=[marker(NOW-5),event(.67,NOW-90000)]
    profile=accounts.parse({"claude":[{"name":"secondaire","path":str(profile_dir),
                                     "cap":.8,"check_credentials":False}]})["claude"]
    def read(lines):
        events.write_text("".join(json.dumps(e)+"\n" for e in lines))
        book=cost.CostBook(state_dir=str(root/"state"),prices_path=str(root/"prices.json"),
                           codex_sessions=str(root/"codex"),db=None,
                           clock=lambda:NOW,tools={"same-agent":"claude"})
        result=accounts.evaluate(book,profile[0],profile,NOW,record=False,critical=True)
        return {"ok":result.ok,"reason":result.reason,
                "gauges":{g.key:{"used":g.used,"produced":getattr(g,"produced_at",None)}
                          for g in result.gauges}}
    before=read(fresh)
    after=read(fresh+replay)
    assert before["ok"] is False
    assert after["ok"] is True
    assert after["gauges"]["seven_day"]["used"] == .67
    assert after["gauges"]["seven_day"]["produced"] is None
    print(json.dumps({"case":"fresh then replay in one journal","before":before,"after":after}))

# P2.2: derive the actual record_gauges predicate without executing its INSERT.
# Evaluate it over VALUES only, with a fixed clock; no table data is read/written.
class Capture:
    def query(self,sql,params):
        self.sql,self.params=sql,params
        return []
cap=Capture()
Operations(cap).record_gauges([{"harness":"claude","account":"secondaire","key":"seven_day",
    "used":.87,"resets_at":RESET,"window_s":7*86400,
    "produced_at":NOW-30,"source":"newest"}])
body=cap.sql[cap.sql.index("SELECT %s"):]
body=body[:body.index("RETURNING id")].replace("now()",f"to_timestamp({NOW})")
cte=f"""WITH quota_gauge_readings(harness,gauge_key,used,resets_at,window_s,account,
                                 produced_at,source,observed_at,id) AS (VALUES
 ('claude'::text,'seven_day'::text,0.67::float8,to_timestamp({RESET}),604800::float8,
  'secondaire'::text,to_timestamp({NOW-3600}),'fresh'::text,to_timestamp({NOW-2}),1::bigint),
 ('claude'::text,'seven_day'::text,0.87::float8,to_timestamp({RESET}),604800::float8,
  'secondaire'::text,to_timestamp({NOW-7200}),'late-stale'::text,to_timestamp({NOW-1}),2::bigint)
) """
from ameesh import config, db
connection=db.connect(config.load())
try:
    rows=connection.query(cte+body,cap.params)
    assert rows == [], rows
    latest_capture=Capture()
    Operations(latest_capture).gauge_latest(harness="claude",account="secondaire")
    latest=connection.query(cte+latest_capture.sql,latest_capture.params)
    assert len(latest)==1 and float(latest[0]["used"]) == .67, latest
    print(json.dumps({"case":"newest production dropped by deduplication",
                      "insert_candidate_rows":len(rows),"history_used_still":.67,
                      "newest_used":.87}))
finally:
    connection.close()

# P2.3: inspect the full interface; no author suite is rerun.
c=C.load()
operations=set()
for domain,field in I.Storage.__annotations__.items():
    cls=getattr(I,field,None) if isinstance(field,str) else field
    if hasattr(cls,"__abstractmethods__"):
        operations.update(domain+"."+name for name in cls.__abstractmethods__)
classified=set(c.operations)|{d+"."+m for d,methods in c.refused.items() for m in methods}
missing=operations-classified
assert "operations.gauge_latest" in missing, missing
assert c.interface_total == 385
# Other missing methods predate this delta; report only its new omission.
print(json.dumps({"case":"interface classification incomplete","missing":sorted(missing),
                  "actual":len(operations),"declared":c.interface_total}))

# P2.4: new PgTestCase module omitted from the required psql pilot selection.
pilot=Path("tests/parts/pilote-psql.txt").read_text()
assert "test_1132_jauges_perimees" not in pilot
print(json.dumps({"case":"psql pilot missing","module":"test_1132_jauges_perimees"}))
