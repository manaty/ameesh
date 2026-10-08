# Design documents

The design of ameesh is written down in the repository, under
[`docs/design/`](https://github.com/manaty/ameesh/tree/main/docs/design).
These documents are **in French**.

| Document | Content |
|---|---|
| [`specification.md`](https://github.com/manaty/ameesh/blob/main/docs/design/specification.md) | the v1 specification: scope, architecture, canon, execution, threads, actions and gate, receipts, ameesh-approve, storage, security guarantees, lot plan |
| [`exigences.md`](https://github.com/manaty/ameesh/blob/main/docs/design/exigences.md) | the requirements |
| [`decisions/`](https://github.com/manaty/ameesh/tree/main/docs/design/decisions) | the owner's decisions, one file each (OKF `Decision` cards) |
| [`etudes/`](https://github.com/manaty/ameesh/tree/main/docs/design/etudes) | studies behind some decisions |
| [`ameesh-approve-hebergement-equipes.md`](https://github.com/manaty/ameesh/blob/main/docs/design/ameesh-approve-hebergement-equipes.md) | the per-team hosting contract of ameesh-approve |
| [`questions-ouvertes.md`](https://github.com/manaty/ameesh/blob/main/docs/design/questions-ouvertes.md) | open questions |

Some decisions that shape what you read in this documentation:

| Decision | In short |
|---|---|
| 0005 | The canon is OKF in git, federated with OKF Federation; other tools are views or proposal interfaces. |
| 0006 | Every exchange is readable and auditable by humans; no hidden channel. |
| 0012 | No owner key on the workstation; human authority goes through ameesh-approve. |
| 0013 | License: AGPL-3.0-only, with a contribution agreement. |
| 0014 | Orchestrators become turn-based agents; placement is a human decision within host policies. |
| 0016 | Postgres in v1, all SQL behind a storage interface, SQLite later for a personal profile. |
| 0018 | Speed: review proportional to risk, interruption, session rotation, measured delays. |
| 0019 | Budgets and routing: model and effort per task, caps, plan gauges read at the source. |
| 0025 | One fresh session per lot for build and review agents. |
| 0026 | ameesh-approve is hosted per team, in one of three modes, with a host name and RP ID of its own. |
| 0028 | Each runner watches its host's resources; under pressure it holds new turns, and can move an agent to another admitted host between two turns. |
| 0029 | A persona (identity, role, responsible human) is declared in the canon; a session is an execution of it, placed by ameesh. |
| 0030 | No work without a way to wake the agent: explicit agent mode, guarded assignment, adopt and resume, pushed alerts, identity never from the folder. |
| 0031 | Several canons on one host: a list of canons, identity by federation, each sync bounded to its canon, names global to their first declarer. |

The design documents are the reference when they and this documentation
disagree; please open an issue if you find such a disagreement.
