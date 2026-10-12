---
type: Study
title: "Study — ameesh on Linux, macOS and Windows"
description: "What fails, or works without protecting, outside Linux; why stay in Python rather than rewrite in Rust; an ameesh.plateforme layer that refuses when a guarantee is missing; the owner's answers of 2026-10-12 (WSL2 first, psutil required, no bypass for secret files and open sessions, remote database, uv tool install, no Rust); lots L63–L69."
status: draft
tags: [portabilite, macos, windows, plateforme, psutil, securite, rust]
generated: { by: "claude3/claude-opus-5-5", at: "2026-10-10T18:00:00+02:00" }
sources:
  - { resource: "etudes/orchestration-et-vivacite.md", title: "Orchestration and liveness study (L39 adoption, L41 hook bindings)" }
  - { resource: "decisions/0030-pas-de-travail-sans-reveil-possible.md", title: "Decision 0030" }
  - { resource: "decisions/0028-ressources-des-hotes-et-repartition.md", title: "Decision 0028 (host resources)" }
  - { resource: "../../src/ameesh/migrations/0036_corrections_de_relecture.sql", title: "Migration 0036 (pid_start)" }
  - { resource: "../../.github/workflows/tests.yml", title: "CI" }
  - { resource: "ameesh decisions #9411-#9417", title: "Owner answers to questions 1-6, 2026-10-12 (#9416 signed)" }
---

# Need

Owner's need decision, 2026-10-10: ameesh must work on **Linux, macOS and
Windows**. A human who drives their agents from a Mac or a Windows
workstation must be able to install ameesh, start the runner, adopt a
session and receive their alerts, with the **same security guarantees** as on
Linux, or an explicit refusal when a guarantee does not exist.

This study does not settle the server question (hosted PostgreSQL database,
v2): it covers what runs on the workstation.

# Measured state (origin/main, 2026-10-10)

Linux is the only tested platform: CI runs on `ubuntu-latest` only
(`.github/workflows/tests.yml:22`), as a matrix over the two drivers
(psql, psycopg). No run has happened on macOS or Windows; what follows comes
from reading the code.

The underlying defect: many functions read `/proc` and, when `/proc` does not
exist, **return an empty result instead of an error**. Depending on the
caller, that empty result becomes either a refusal (safe but unusable) or an
authorization (dangerous).

## macOS: ameesh starts, but some protections protect nothing

| Item | Where | Effect on macOS | Severity |
|---|---|---|---|
| "Fail-open" adoption | `reprise.holders` (`reprise.py:161`) walks `/proc/*/fd`; `os.listdir("/proc")` fails → `return []`; the caller (`reprise.py:488`) refuses only if the list is non-empty | `ameesh adopt` / `resume` **never** sees a session that is still open: two harnesses can write the same session | **Security / integrity** |
| Hook bindings by PID (L41) | `session_bindings.ancestors` (`session_bindings.py:55`) climbs `/proc/<pid>/status`; without `/proc`, the chain shrinks to the process itself | `for_hook` never finds the harness PID in the ancestry: every `--pid` binding is refused. Safe, but unusable | Functional |
| Start time (L46) | `session_bindings.pid_start` (`:77`) reads field 22 of `/proc/<pid>/stat` → `None` | The binding is recorded without `pid_start`; `_same_process` then treats the check as satisfied: the protection against a recycled PID is **inactive** | Security (mitigated by the previous item) |
| `whoami` / `send` in a bound session | `session_bindings.by_ancestry` (`:350`) depends on `ancestors` | Never finds the binding: identity not resolved | Functional |
| Host resources (0028) | `resources._meminfo` reads `/proc/meminfo` (`resources.py:101`) | No available memory and no swap: the allocation decides without these measures | Functional |
| Services | `deploy/systemd/` only | No launchd unit: the runner and `ameesh notify` must be started by hand | Operations |
| Desktop notification | `notify.py:562` looks for `notify-send` | Desktop channel fails ("notify-send not found") | Functional |
| Human's terminal | `cli.find_tty` (`cli.py:146`) reads `/proc/<pid>/fd` and accepts only `/dev/pts/*` | No tty is ever found (macOS: `/dev/ttys*`, no `/proc`) | Minor |
| Account credentials | `accounts.py:307` skips the credentials file check on `darwin` (Claude Code stores them in the Keychain) | A non-logged-in account passes the check; a deliberate gap, but not verified otherwise | Minor |
| Tests | 7 lines in `tests/` hardcode `/proc`; `test_l16_harnais.py:243` skips cleanly, but `tests/test_runner.py:640` and `:680` expect `not os.path.exists("/proc/<pid>")`, which is **always true** without `/proc`: these two tests would pass while checking nothing; `test_runner.py:252` would fail | False confidence if macOS is added to CI without fixing them | Quality |

## Native Windows: ameesh does not start

| Item | Where | Effect |
|---|---|---|
| `import fcntl` at module top | `fil.py:47`, imported by `cli`, `mail`, `backend`, `actions`, `notify`, `main`… | `ImportError` at launch of almost every command |
| Process groups | `runner.py`: `start_new_session=True` (`:1829`, `:2887`), `os.getpgid` (`:447`), `os.killpg` (`:456`, `:470`), SIGTERM then SIGKILL | Absent on Windows: the runner can neither isolate nor kill a harness's process tree |
| Secret file permissions by mode bits | `signing.read_private` (`signing.py:403`), `notify.py:431`, `harnesses._component_problem` (`harnesses.py:224`), `approve/config._check_private` (`approve/config.py:428`, `os.geteuid`) | On Windows `st_mode` is commonly `0o666`: **systematic** refusal (or `AttributeError` for `geteuid`). Safe, but nothing works; and the real protection (ACL) is not checked |
| Opening without following links | `O_NOFOLLOW` / `dir_fd` in `acp.py`, `fil.py`, `harnesses.py`, `approve/tls.py`, `approve/store.py`, `approve/config.py` | `O_NOFOLLOW` does not exist, `dir_fd` is not supported |
| psql driver | `db.py:450`: `select.select` on the `psql` pipe | On Windows, `select` accepts only sockets: the default driver does not work (psycopg does) |
| System load | `resources.load1` (`resources.py:137`) catches `OSError`, not the absence of `os.getloadavg` | Uncaught `AttributeError` |
| Journal locks | `fil.py:620`, `:656` (`fcntl.flock`); `runner.py:135`, `notify.py:1073` | To be replaced by `msvcrt.locking` or `portalocker` |

WSL2 runs ameesh as is (it is a Linux); this is the immediate route for a
Windows workstation, but the harness must then also run inside WSL (the open
session check does not see Windows processes).

# Option set aside for now: rewrite in Rust

A rewrite in Rust was considered (single binary, no interpreter to install,
fast startup). It is **set aside for now**:

1. **The gaps are OS semantics, not language.** Rust has no `/proc` on macOS
   and no `killpg` on Windows. The same per-OS backends would have to be
   written (ancestry, open files, process groups / Job Objects, ACLs, locks,
   launchd / Windows services). The existing crates (`sysinfo`, `nix`,
   `windows`) play the role that `psutil`, `os` and `ctypes` play in Python.
2. **The measured slowness does not come from Python.** One round trip to the
   remote database costs 183 ms; importing ameesh takes 0.09 s. The network
   round trips are what must be reduced (work in progress, L61), not the
   interpreter.
3. **Cost and risk.** About 40,000 lines of Python, more than 1,400 tests, a
   dozen lots in flight (L50–L62); the most delicate parts (signed receipts,
   human authority, runner leases, COSE/JCS) would be rewritten and would
   have to be revalidated byte for byte.

Rust remains possible **later, component by component**, behind interfaces
that are already stable (SQL schema, JSON outputs, receipt formats):

* first the `agent-mail hook` hook, called on **every** harness event: a
  native binary removes the Python startup cost there and the need to install
  an interpreter on the workstation;
* possibly the runner, if it becomes a multi-OS system service.

## Link with Nexlink Compute

Parallel study of 2026-10-10 (specification `docs/specs/nexlink-compute.md`
in the Nexlink repository, lots NX-COMPUTE-1a to 1d).

* **What Compute is.** A consenting device publishes an offer signed by its
  P-256 device key. The hub places jobs on it under a lease (900 s), and the
  device stops by itself at expiry. Three workloads are planned, in this
  order: CI builds, **ameesh agent turns**, page containers. The first tier is
  limited to "my devices", for example the Windows and Mac PCs of a family.
* **On the device side, there is no native multi-OS agent.** The software is
  the Nexlink desktop application: Electron/TypeScript, already signed and
  notarized on macOS, signed on Windows, published as Linux packages, with
  automatic update. The Compute runner is a TypeScript module. Execution
  happens in a rootless container without network on Linux, and in a VM on
  macOS and Windows. This VM is not written yet (lot 1c-iii), and its choice
  is not settled: Lima, WSL2…
* **Consequence for this choice.** The assumption of a native agent that
  ameesh could build on does not hold. The OS primitives remain to be written,
  and the `ameesh.plateforme` layer (L63) keeps its priority. Nothing pushes
  toward rewriting ameesh in Rust either: Compute is in TypeScript, and its
  only native binary (the Rust sidecar of remote-help) is not used by Compute.
* **Two uses to distinguish.**
  1. **The human on their own Mac or PC**: they drive, attach and receive
     their notifications. ameesh runs natively there, which is the subject of
     this study.
  2. **A workstation lent as a host through Compute**: agent turns run there
     under Linux, in the Compute VM. ameesh then needs only Linux, but it
     depends on the server-mediated runner API (ameesh v2), which does not
     exist yet.
* **To share**:
  * a single VM design per OS (1c-iii), which also serves as the "Windows via
    WSL2" option of L67;
  * the signing and notarization chain already in place in Nexlink, which L69
    reuses instead of rebuilding;
  * a single lease model, whereas there are two today (300 s for ameesh, 900 s
    for Compute);
  * a single device identity (the Nexlink device key as the identity of the
    Host card?);
  * a single measure of resources and back-pressure (0028 against Compute's
    offer and continuous adaptation).
* **Friction point.** The visibility rule of
  [0029](../decisions/0029-persona-et-session.md) (a persona runs only for a
  human who has access to its repository) excludes a `public` audience for
  agent turns.

# Proposed choice

## A single `ameesh.plateforme` layer

Everything that depends on the OS goes through a single module,
`src/ameesh/plateforme/` (one submodule per subject, one backend per OS). No
`if sys.platform` anywhere else in the code; a lint test checks it (`grep` for
`sys.platform`, `/proc`, `fcntl`, `os.killpg`, `O_NOFOLLOW` outside
`plateforme/`).

| Subject | Interface | Linux | macOS | Windows |
|---|---|---|---|---|
| Processes | `ascendance(pid)`, `demarrage(pid)`, `ligne_de_commande(pid)`, `detenteurs(chemin)` (open files) | psutil (or `/proc` as fallback) | psutil (`proc_pidinfo`, `lsof`-like) | psutil |
| Start time | epoch seconds (float), **normalized** | `psutil.Process.create_time()` | same | same |
| Locks | `verrou(fichier, exclusif)` | `fcntl.flock` | `fcntl.flock` | `msvcrt.locking` (or `portalocker`) |
| Process group | `lancer_isole(argv)`, `tuer_arbre(h, dur)` | `setsid` + `killpg` | same | `CREATE_NEW_PROCESS_GROUP` + **Job Object** (`KILL_ON_JOB_CLOSE`) |
| Secret files | `verifier_prive(chemin)`, `rendre_prive(chemin)` | mode bits + owner | same | ACL: only the current user (and SYSTEM) has access; checked via `pywin32` or `icacls` |
| Opening without links | `ouvrir_sans_lien(dossier, nom)` | `O_NOFOLLOW` + `dir_fd` | same | `FILE_FLAG_OPEN_REPARSE_POINT` then a type check; otherwise **documented refusal** |
| Resources | `memoire_disponible()`, `swap_utilise()`, `charge()` | psutil / `/proc/meminfo` | psutil | psutil (load: `None`, no reliable equivalent) |
| Desktop notification | `notifier(titre, texte)` | `notify-send` | `osascript -e 'display notification'` | Windows toast (PowerShell / `BurntToast`, or WinRT API) |
| Services | `installer_service(nom, argv)` | systemd user | launchd (`~/Library/LaunchAgents`) | Windows service or Task Scheduler |
| Terminal | `tty_de_l_humain()` | `/dev/pts/*` | `/dev/ttys*` (psutil `terminal()`) | `None` |
| Paths | `config_dir()`, `state_dir()` | `~/.config/ameesh`, `~/.local/state/ameesh` | **same paths** (one doc, one behavior) | same paths under `%USERPROFILE%`; `platformdirs` optional |

**psutil** becomes a dependency. Today `dependencies = []`
(`pyproject.toml:16`): this is a change of principle, to be confirmed
(question 2). psutil is maintained, published as binary wheels for the three
OSes, and covers ancestry, open files, start time, terminal and memory. On
Linux, the current `/proc` fallback stays possible if we want to keep an
install with no dependency.

## Start time: watch out for 0036

`session_bindings.pid_start` today stores **clock ticks since the host
booted** (field 22 of `/proc/<pid>/stat`, migration 0036). psutil returns
**epoch seconds**. The two are not comparable: without care, all existing
bindings would be seen as "PID reused". Proposal: a new column
`pid_started_at` (`double precision`, epoch) written by the platform layer;
`pid_start` stays read for old bindings on Linux (conversion
`boot_time + ticks / CLK_TCK`, one-second tolerance), then is dropped once no
active binding carries it. Migration to be numbered (see the plan).

## Rule: refuse when a guarantee is missing

When a security guarantee is not available on an OS, ameesh **refuses**
(fail-closed) instead of letting it pass. Concretely:

* each `plateforme` function that cannot answer raises
  `NonDisponible(sujet, os, raison)`; it **never** returns an empty list or a
  `None` that would mean "nothing found";
* the caller decides: refusal with a message ("cannot verify that the session
  is closed on this OS"), unless an explicit `--force`, which is **logged**
  (event and finding, like the `--force` of `adopt` today);
* non-sensitive measures (load, terminal) may return `None`: this is
  documented subject by subject.

The first case to fix is `reprise.holders`: without `/proc` and without
psutil, `adopt` must refuse, not adopt.

# Proposed lot plan

L60–L62 are taken (DeepSeek rotation, fast CLI, project view). The
`lot/l5*` branches go up to migration 0040; no `lot/l6*` branch carries a
migration yet, but L60 may take **0041**: the migrations of this plan will be
numbered at merge (0041 or the next free one). Each lot: branch
`lot/l6x-…`, tests, independent review, merge with the owner's agreement.

| Lot | Content | Depends on | Size |
|---|---|---|---|
| **L63** — platform layer and adopt fix (**priority, security**) | `ameesh.plateforme` (processes, start time, terminal) on psutil; `NonDisponible`; `reprise.holders`, `session_bindings.ancestors` / `pid_start` / `by_ancestry`, `cli.find_tty` go through it; **adopt refuses** when no check is possible (except a logged `--force`); `pid_started_at` migration + compatible read of `pid_start`; lint "no `/proc` or `sys.platform` outside plateforme" | — | M |
| **L64** — resources | `memoire_disponible`, `swap_utilise`, `charge` through the layer; `load1` guards against the absence of `getloadavg`; `ameesh doctor` says what is not measured | L63 | S |
| **L65** — macOS: services and installation | launchd units (runner, `ameesh notify`) generated by `ameesh service install`; macOS installation doc (PostgreSQL via Homebrew or Docker, or remote database); credentials check in the Keychain (`security find-generic-password`) instead of the skip at `accounts.py:307` | L63 | M |
| **L66** — desktop notifications | desktop channel via `osascript` (macOS) and toast (Windows); `doctor --notify-test` per OS | L63 | S |
| **L67** — native Windows | locks (`msvcrt` / `portalocker`) in `fil`, `runner`, `notify`; `import fcntl` removed from module tops; launch and stop through Job Objects; secret file checks by ACL (`signing`, `notify`, `harnesses`, `approve/config`); opening without links or documented refusal (`acp`, `fil`, `approve/tls`, `approve/store`); psql driver without `select` on a pipe (reader thread) or psycopg required on Windows; harness sandbox: what exists, what is refused; Windows services | L63, L64 | L |
| **L68** — multi-OS CI | `ubuntu-latest` / `macos-latest` / `windows-latest` matrix (PostgreSQL as a service on Linux, installed in a step on macOS and Windows); `/proc` tests rewritten through the layer (`test_runner.py:252`, `:640`, `:680`, `test_l16_harnais.py:243`); a test that fails if a guarantee function returns empty instead of raising | L63; continuous with L65–L67 | M |
| **L69** — distribution | `uv tool install ameesh` documented on the three OSes; optional packaged executable (PyInstaller or equivalent); macOS signing and notarization, Windows Authenticode signing; publication from CI | L68 | M |

Recommended order: **L63 first** (it closes a real hole on macOS as soon as a
human runs `adopt` there); then L64, L65, L66 and L68 in parallel; L67 after
that; L69 last. Until L63 lands, the doc should say that macOS is not
supported and that Windows goes through WSL2.

# Questions for the owner

Questions 1 to 6 were put to the owner through `ameesh decide ask` on
2026-10-12 (decisions #9411 to #9417) and answered the same day; the answer
to question 5 is signed. Questions 7 to 11 were not asked: 7 lapsed when L63
merged (`6fafe37`), and 8 to 11 belong with the Nexlink Compute work and stay
in the [open questions](../questions-ouvertes.md).

1. **Windows scope**: native Windows (L67, the biggest lot) or WSL2 only at
   first?
   **Answer (#9411, a)**: WSL2 only at first; native Windows (L67) later.
2. **psutil required**: accept a first required dependency, or keep it
   optional (`ameesh[plateforme]`) with the `/proc` fallback on Linux and a
   refusal elsewhere?
   **Answer (#9412, a)**: required, so that one implementation runs on the
   three OSes. L63 already made it a dependency (`pyproject.toml`); the
   `/proc` fallback stays a degraded path on Linux, and no second
   implementation is written for macOS or Windows.
3. **`--force` outside Linux**: is a logged `--force` enough when a guarantee
   is missing, or must some (secret files, open session) have no bypass?
   **Answer (#9413, b)**: no bypass for secret files nor for the open session;
   a logged `--force` for the rest. L63 as merged still lets `adopt --force`
   through when the OS cannot check that the session is closed
   (`reprise.py`, `platform.NotAvailable`): L63b removes that bypass. The
   `--force` that predates L63, on a session that Linux *does* see open, is
   outside this question and stays logged as before.
4. **Database on the workstation**: on macOS and Windows, local PostgreSQL
   (Homebrew, Docker, installer) or remote database only (the team's server)?
   **Answer (#9414, a)**: remote database only. L65 documents no local
   PostgreSQL on macOS; CI still installs one to run the suite.
5. **Distribution**: is `uv tool install` enough, or is a signed executable
   needed (Apple developer account, Windows signing certificate: cost and
   holder)?
   **Answer (#9416, a, signed)**: `uv tool install` is enough for now. L69
   shrinks to documentation; no signing account is opened.
6. **Rust**: confirm "not now". The Nexlink Compute study gives no reason to
   switch: Compute is in TypeScript, with no native agent. The native hook
   remains the first candidate, later.
   **Answer (#9417, a)**: confirmed, no Rust now.
7. **Order**: L63 before the L50–L62 lots still in flight, or after them?
   *Lapsed: L63 is merged.*
8. **Intended use**: the human on their own workstation, a workstation lent as
   a host through Nexlink Compute, or both? The second depends on the mediated
   runner API (ameesh v2): should it be prioritized?
9. **Windows and the Compute VM**: align the "WSL2" choice of L67 with the
   Compute VM choice (1c-iii)?
10. **Distribution through the Nexlink application**: is ameesh installed and
    supervised by the Nexlink desktop application, or does it stay independent
    ([0003](../decisions/0003-ameesh-et-nexlink.md))?
11. **Sharing with Compute**: a single lease model, the Nexlink device key as
    host identity, a single measure of resources? And who arbitrates the load
    of a workstation that is both an ameesh host and a Compute provider?

# Lot plan after the answers (2026-10-12)

The [proposed plan](#proposed-lot-plan) above is kept as written on
2026-10-10. Measured since then: L63 merged (`6fafe37`); L159d and L159e
already route host memory, swap, load and pressure through
`ameesh.platform` (psutil, `None` for load without `os.getloadavg`); CI has a
`platform-macos` part, limited to the platform tests without a database.
WSL2 is a Linux: what runs on Linux runs there, except reaching the Windows
desktop.

| Lot | Content after the answers | Depends on | Estimate |
|---|---|---|---|
| **L63b** — no bypass for an unverifiable session (Q3) | `adopt` and `resume` refuse, even with `--force`, when `platform.NotAvailable` prevents checking that the session is closed; the message names the OS and the missing check; test on a platform without `/proc` nor psutil; check that no secret-file check (`signing`, `notify`, `harnesses`, `approve/config`) accepts a `--force` | L63 | 1 h |
| **L64** — unmeasured resources said | the measures already go through the layer; left: `ameesh doctor` (and `ameesh show` for the host) lists the measures this OS cannot give (load, PSI, cgroup, btrfs) instead of publishing nothing silently | L63 | 1 h |
| **L65** — macOS: services and installation | launchd units (runner, `ameesh notify`) generated by `ameesh service install`; macOS installation doc with a **remote database only** (Q4); credentials check in the Keychain (`security find-generic-password`) instead of the skip in `accounts.py` | L63 | 4 h |
| **L66** — desktop notifications | `osascript` on macOS; under WSL2, a Windows toast through `powershell.exe` (interop), since `notify-send` reaches no Windows desktop there; `doctor --notify-test`; the native Windows toast goes with L67 | L63 | 2 h |
| **L67** — native Windows (**deferred**, Q1) | content unchanged (locks, Job Objects, ACLs, opening without links, psql driver, services); when it starts, the secret-file ACL check has no bypass (Q3). Opened without planned dates | L63, L64 | 2 d |
| **L68** — CI on macOS | the macOS part runs the whole suite against a PostgreSQL installed in a step (the workstation's remote-only rule does not apply to CI); `/proc` tests rewritten through the layer (`test_runner.py`, `test_l16_harnais.py`); a test that fails if a guarantee function returns empty instead of raising. No Windows part before L67 | L63 | 3 h |
| **L69** — distribution | `uv tool install ameesh` documented for Linux, macOS and Windows through WSL2 (harness inside WSL too, see above); no packaged or signed executable for now (Q5) | L65 | 1 h 30 |

No Rust lot (Q6). Order: L63b first (security), then L64, L65, L66 and L68 in
parallel, L69 after L65; L67 waits for an owner decision to start it. Until
L65 and L68 land, the doc says that macOS is not yet supported and that
Windows goes through WSL2.
