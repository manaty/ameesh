# Run the demo

`scripts/demo-v1.sh` plays the end-to-end scenario of v1 on the bench,
commented step by step. It is the same scenario as the end-to-end test
(`tests/test_bout_en_bout.py`).

```bash
export PGPASSWORD=…          # the bench password (or ~/.pgpass)
scripts/pg-up.sh             # if the bench container is not running
scripts/demo-v1.sh
```

!!! info "Nothing real is touched"
    The organisation is **fictitious** (`acme`), the harnesses are fake
    binaries (`tests/fakebin`, never `claude`, `codex` or `dsh`), `gh` is fake
    (never GitHub), and the passkey is a **software** authenticator used only
    by tests and the demo. ameesh-approve listens on `127.0.0.1` only; the
    HTTPS proxy and the phone are simulated. Everything else goes through the
    real ameesh commands.

`AMEESH_DEMO_DSN` sets the bench's admin database (default: `AMEESH_TEST_DSN`,
else `postgresql://agent_mesh@127.0.0.1:55432/agent_mesh`). The demo creates a
temporary `ameesh_demo_…` database (or a schema of that name if creating a
database is refused) and drops it on exit, even on error or Ctrl-C. It unsets
every `AMEESH_*`, `AGENT_MESH_*`, `AGENT_MAIL_*` and `GIT_*` variable of your
shell first, so it never touches a real setup. The budget guard stays active,
but the Codex subscription gauges are read from an empty `CODEX_HOME` created
for the demo (and deleted with it), never from your machine's real harness
logs: a busy real plan would otherwise pause the fake agents.

The demo's own comments and the CLI output are in French.

## The steps

| Step | What happens | Commands |
|---|---|---|
| 0. A temporary database | migrations applied | `ameesh migrate` |
| 1. The example canon in a git repository | a bare repository stands for the canonical remote; ameesh reads `origin/main` through git objects | `ameesh canon check` |
| 2. Canon → registry of host `atelier` | the canon state becomes `ok`; agents become claimable; the passkey registry is bootstrapped from `main` | `ameesh canon sync --bootstrap-ref main`, `ameesh show orchestre` |
| 3. The runner claims two fake agents | `alice` writes to both; one pass of the runner gives each a turn, with the identity bound to its lease | `ameesh mail send`, `agent-runner --once`, `ameesh list` |
| 4. Agent-to-agent message, readable | `orchestre` writes to `relecteur`; the message appears verbatim in the `acme-web` thread; the next pass wakes `relecteur` | `ameesh mail send`, `ameesh fil show acme-web` |
| 5. An irreversible `git-merge` action | proposed by `orchestre`; execution is **refused** without a receipt; it waits in alice's decisions | `ameesh action propose`, `ameesh action execute` (exit 1), `ameesh decisions --for human:alice` |
| 6. ameesh-approve and alice's passkey | service token, service on an ephemeral loopback port, enrolment link; the "phone" creates a passkey, which is only a **proposal**; the entry is added to `membres/alice.md` by a commit standing for a merged pull request; `canon sync` activates it | `ameesh-approve gen-token`, `serve`, `enroll-link`, `ameesh authenticator list`, `ameesh canon sync` |
| 7. Request, signature, receipt | ameesh sends the request; until it is signed, fetching returns 5; the "phone" signs the summary recomputed by the service; ameesh fetches and **verifies** the receipt itself | `ameesh action request`, `ameesh action fetch-receipt`, `ameesh receipt verify` |
| 8. Execution, then the answer is lost | the fake `gh` merges, then the connection drops: outcome **unknown** (exit 4), never retried automatically; executing again is refused | `ameesh action execute` |
| 9. Reconciliation | the connector reads the pull request's state: `confirmed`; every transition is in the thread; the pull request was merged exactly once; replaying the receipt fails with `[replay]` | `ameesh action reconcile`, `ameesh action show`, `ameesh fil show acme-web --last 6` |

## What to look at

- In step 4, the thread entry: heading `agent:orchestre → agent:relecteur`,
  then the text, fenced.
- In step 5, the digest printed by `propose`: it is the value the human signs
  in step 7, and that `receipt verify --digest` checks.
- In step 9, the thread: each transition (proposed, approval requested,
  approved, launched, unknown, confirmed) is a readable entry.
