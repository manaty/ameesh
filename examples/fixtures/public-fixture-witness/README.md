# Public fixture witness

An installed external consumer of `ameesh.public.fixture` (contract revision
1.5, [#7701](https://github.com/manaty/ameesh/issues/5902), with its agent
states of revision 1.7, [#8341](https://github.com/manaty/ameesh/issues/6581)) and
`ameesh.public.database` (revision 1.6,
[#6573](https://github.com/manaty/ameesh/issues/6573)). This directory is its
own distribution, outside `src/`: it imports only `ameesh.public.fixture`,
`ameesh.public.mail`, `ameesh.public.database` and the Python standard library,
and runs the installed `ameesh`/`agent-mail` executables as subprocesses. It
never imports `tests.support` or a private ameesh module, and it writes no core
table: the fixture's only SQL read surface is the three admitted named views.

## What it proves

The witness creates a consumer-owned disposable database
(`ameesh_fixture_*`) from an admin DSN, migrates it through `ameesh migrate`,
and then, on that database only:

- admits a consumer Process card (`ameesh process check`), refuses a broken
  card with its named reason (`edge-undeclared`), adopts a Pipeline module and
  observes the verifier refusal (`verifier-refused`) and acceptance
  (`planning`) plus the resulting `ameesh work show` journal;
- reads native usage and accounting (`ameesh cost turns`, `ameesh budget`);
- submits, finds and reads mail through `ameesh.public.mail`, including the
  reserved-actor, unknown-recipient and idempotency-conflict refusals, one
  identical-key race (one durable row, one reuse) and one conflicting-key race
  (one success, one `IdempotencyConflict`);
- reads only the admitted named views with explicit columns;
- runs a clean child process of the fixture environment that calls
  `ameesh.public.database.connect()` with no argument, submits and reads through
  `ameesh.public.mail`, closes, and observes the documented schema, storage and
  unavailable refusals without exposing the DSN;
- registers an agent with `Fixture.agent()` and puts it into two qualification
  states and back with `Fixture.agent_state()` (`lease_expired` then
  `lease_free`, `canon_stale` then `canon_fresh`), and sees an unknown state
  refused with code `refused`. It writes no core table to get there.

It prints one JSON report and exits 0 only if every check passed. The fixture
drops its database and its private root before the report is printed, so the
`teardown` section is the evidence that nothing was left behind.

## Run it

Install ameesh in an environment that can reach the PostgreSQL server, then set
an admin DSN whose role may `CREATE DATABASE`:

```sh
export AMEESH_FIXTURE_ADMIN_DSN='postgresql://role@127.0.0.1:5432/postgres'
python fixture_witness.py
```

Use a role with `CREATEDB`; the fixture only creates and drops names starting
with `ameesh_fixture_`. The admin DSN is a secret: the fixture passes it to the
child environment and never prints it. `AMEESH_DSN` is deliberately not read
here, because it may name a live mesh database.

The witness uses `psycopg` when it is importable and `psql` otherwise; both
drivers produce the same report. `tests/test_7701_public_fixture.py` runs this
witness in CI with `AMEESH_TEST_DSN` and checks the report, the prefix and the
dropped database.
