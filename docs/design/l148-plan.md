# L148a — French code identifiers on `origin/main`: inventory and conflict-free rename plan

Status: **plan, no code**. Work item **#469 (L148a)**, preparation for **#147 (L148)**.
Author: `mty-amh-impl-04` (implementation agent, DeepSeek harness), 2026-10-12.
Base: `origin/main` at `4a056a97` (2026-10-12). Language: English, as requested.

Revision, 2026-10-12 17:54 UTC: `origin/main` has moved to `27ef6b49` (PR #216, Path B, merged
just after the snapshot). The inventory below was re-anchored on `27ef6b49`: the 54 `file:line`
references that had moved are updated, the three migrations and eight tables #216 added are
counted (68 migration files, 64 tables, still no French name among them), and the new
definitions and CLI options #216 introduced were checked: all English. What remains a
`4a056a97` snapshot is §3's open-PR table, the per-file open-PR columns of §2.2/§2.5 and the
`refs` column: #216 no longer counts in the conflict set (it is removed from stage 4's wait
list), and the set must be re-read with `gh pr list` before each stage (§6).

This document is the deliverable of #469. It inventories the French identifiers still on
`origin/main`, maps each of them to the open pull requests that touch the same files, and
proposes an order of small, conflict-free PRs with the compatibility aliases each one needs.
It changes no code.

## 0. Summary

- **Production code (`src/ameesh`):** 85 French function/class names, 5 French module names.
- **Tests:** 2 090 French function/class names in 155 files (mostly `test_*` sentence names), plus 22 French module names.
- **Dev scripts:** 28 French function names in 4 files, plus 4 French file names.
- **Database:** 1 French column outside the kept domain term (`agent_registry.chantier`); 64 tables, all English. No French table name.
- **Migrations:** 14 applied migration file names are French. **They are never renamed**; new migrations get English names.
- **CLI:** 27 French options (several already have an English alias), 11 French subcommands, 6 French positional names.
- **Config:** no French configuration-file key; 6 canon model fields named `fiche`. The `organisation` key is British English, not French-only.
- **Adjacent:** ~17 French JSON/API keys and 6 stored value vocabularies.
- **Open PRs:** 16 at 2026-10-12 17:2x UTC. Nine of them touch `runner.py`, which alone holds
  37 of the 85 production names: `runner.py` must be renamed **last**, after those PRs merge.
- **Proposed plan:** 7 stages; stages 1a and 2 touch only files no open PR touches, stage 1b
  waits on three small PRs, stage 3 renames `chantier` with a dual-write trigger, stage 4
  handles hot files once their PRs merge, stage 5 is the test suite (needs a scope decision:
  2 090 names), stages 6–7 are API/values and cleanup.

**Key scoping facts found while building this inventory:**

1. `porte_hote`, the first example in the lot body, **does not exist on `manaty/ameesh`
   `origin/main`** (0 occurrences). It lives in `manaty/nexlink`, which #147 does not cover
   (§5.1): this inventory covers the ameesh repository only.
2. The domain term **`lot` is kept** (mesh-design decision, message n°6795 relayed in
   `fils/ameesh/_projet.md` line 4031: keep `lot`, glossed "lot (work item)"). It appears in
   5 columns, 5 CLI options, 1 dataclass field and the `/api/v1/lots` routes; this plan treats it
   as accepted vocabulary and only lists it, it does not rename it. Renaming it would touch most
   of the repository and every open PR.
3. Several French options **already have an English alias** (`--start`, `--end`, `--delivery`,
   `--project`, `--owner`), and `--tache`/`--deploiement` are unused French-only aliases: those
   two can simply be deleted.

## 1. Method

- Function/class names: Python `ast` over `src/`, `tests/`, `scripts/`, `bin/`; French flagged
  by a curated token list reviewed by hand against the full token inventory (1 592 distinct
  tokens), then each candidate read in context to remove English false friends (`retire`,
  `refuse`, `repos`, `trace`, `mise` — the `mise` tool, not French).
- Counts for functions are AST reference counts (definition + `Name`/`Attribute`/keyword/
  argument references) across all code, so French comments and docstrings do not inflate them.
  They are **name-wide**: a French local variable with the same name counts too, so generic
  names (`lire`, `ecrire`, `sortie`, `durees`) overstate call sites; they measure rename churn.
- Modules, SQL objects, migrations, CLI strings and JSON keys: word-boundary regex over code
  (`.py .sql .sh .json .toml .yml .yaml .txt .conf` in `src tests scripts bin deploy .github
  examples`) and, separately, over docs (`docs/`, `README.md`, `MESH-SPEC.md`, `site/`).
- SQL: parsed from `src/ameesh/migrations/*.sql` (`create table`, `alter table ... add column`,
  `create index/trigger/function/view`), which is the schema source of truth on `origin/main`.
- Open PRs: `gh pr list` + `gh api .../pulls/<n>/files --paginate` on 2026-10-12; PRs #216 and
  #394 were stacked (104 and 99 files) and appeared in most `src/` rows; #216 has since merged
  (revision note), #394 is still open.
- Local variables, instance attributes and French strings/comments are **not** enumerated (see §6).

## 2. Inventory

### 2.1 Modules (Python files) and code paths

**Production modules — 5** (reference count = regex occurrences in code):

| file                           | refs | suggested       | open PRs touching it | note                             |
|--------------------------------|------|-----------------|----------------------|----------------------------------|
| src/ameesh/fil.py              | 614  | thread_log.py   | #421                 | readable thread transport (file) |
| src/ameesh/grand_livre.py      | 11   | ledger.py       | —                    | turn_costs corrections           |
| src/ameesh/menage.py           | 358  | housekeeping.py | —                    |                                  |
| src/ameesh/reprise.py          | 264  | resume.py       | #377, #424           | adopt + resume                   |
| src/ameesh/sous_utilisation.py | 88   | underuse.py     | #424                 |                                  |

**Test modules — 22** (file names; the tests themselves are in §2.2 and §4 stage 5):

| file                                  | suggested                               |
|---------------------------------------|-----------------------------------------|
| tests/test_courrier_consigne.py       | test_mail_instruction.py                |
| tests/test_fil.py                     | test_thread_log.py                      |
| tests/test_l106_coupure_hote.py       | test_l106_host_cutoff.py                |
| tests/test_l125_courrier_regroupe.py  | test_l125_mail_batched.py               |
| tests/test_l130_demandes_sans_lot.py  | test_l130_requests_without_work_item.py |
| tests/test_l160c_radar_courrier.py    | test_l160c_radar_mail.py                |
| tests/test_l161b_flux.py              | test_l161b_flow.py                      |
| tests/test_l161c_echecs_temps_reel.py | test_l161c_failures_realtime.py         |
| tests/test_l161e_carte.py             | test_l161e_map.py                       |
| tests/test_l165_prise_lots.py         | test_l165_work_item_take.py             |
| tests/test_l166_session_fille.py      | test_l166_child_session.py              |
| tests/test_l219_menage_tmp.py         | test_l219_housekeeping_tmp.py           |
| tests/test_l39_adoption_reprise.py    | test_l39_adoption_resume.py             |
| tests/test_l41_liaisons.py            | test_l41_bindings.py                    |
| tests/test_l43_perimetre_hote.py      | test_l43_host_scope.py                  |
| tests/test_l48_echecs_rapides.py      | test_l48_fast_failures.py               |
| tests/test_l50_group_orphan_lots.py   | test_l50_group_orphan_work_items.py     |
| tests/test_l62_projets.py             | test_l62_projects.py                    |
| tests/test_l73_menage.py              | test_l73_housekeeping.py                |
| tests/test_l94_sous_utilisation.py    | test_l94_underuse.py                    |
| tests/test_l96_feuille_de_route.py    | test_l96_roadmap.py                     |
| tests/test_suivi_lots_fusionnes.py    | test_merged_work_item_tracking.py       |

**Dev scripts and other code paths — 9**:

| path                        | suggested                      | open PRs touching it   |
|-----------------------------|--------------------------------|------------------------|
| scripts/carte.py            | scripts/test_map.py            | —                      |
| scripts/carte_mesure.py     | scripts/test_map_measure.py    | —                      |
| scripts/ci-flux.py          | scripts/ci-flow.py             | —                      |
| scripts/test-parallele.sh   | scripts/test-parallel.sh       | —                      |
| deploy/mise-a-jour/         | deploy/update/                 | —                      |
| .github/workflows/carte.yml | .github/workflows/test-map.yml | —                      |
| tests/parts/durees.json     | tests/parts/durations.json     | —                      |
| tests/parts/pilote-psql.txt | tests/parts/driver-psql.txt    | #194, #216, #394, #420 |
| examples/canon/hotes/       | examples/canon/hosts/          | —                      |

### 2.2 Functions and classes

**`src/ameesh` — 85 names**, with the file, line, English proposal and AST reference count.

| site                              | French name              | English proposal         | refs | open PRs on the file                                 | note                       |
|-----------------------------------|--------------------------|--------------------------|------|------------------------------------------------------|----------------------------|
| adapters.py:541                   | hors_path                | outside_path             | 3    | #420                                                 |                            |
| backend.py:120                    | chantier                 | workstream               | 89   | #216, #394                                           |                            |
| background.py:82                  | lignee                   | lineage                  | 3    | #216, #394, #428                                     |                            |
| background.py:230                 | boucle                   | loop / <context>_loop    | 12   | #216, #394, #428                                     | 6 local loop functions     |
| canon.py:738                      | Fiche                    | Card                     | 28   | #216, #394                                           |                            |
| canon.py:1144                     | fiche                    | card_dict                | 303  | #216, #394                                           | nested helper in to_dict() |
| canon.py:2005                     | _read_fiches             | _read_cards              | 2    | #216, #394                                           |                            |
| canon.py:2035                     | _fiche                   | _card                    | 3    | #216, #394                                           |                            |
| canon.py:2632                     | host_fiches              | host_cards               | 2    | #216, #394                                           |                            |
| chat.py:73                        | consigne                 | instruction              | 63   | —                                                    |                            |
| cli.py:589                        | lire                     | read                     | 31   | #216, #394, #417                                     |                            |
| cli.py:622                        | _Remise                  | _Delivery                | 2    | #216, #394, #417                                     | mail reservation handover  |
| cli.py:640                        | prendre                  | take                     | 2    | #216, #394, #417                                     |                            |
| cli.py:657                        | solder                   | settle                   | 3    | #216, #394, #417                                     |                            |
| cli.py:665                        | abandonner               | abandon                  | 2    | #216, #394, #417                                     |                            |
| cli.py:678                        | avis_borne               | bounded_notice           | 3    | #216, #394, #417                                     |                            |
| cli.py:686                        | avis_borne_travail       | bounded_notice_work      | 3    | #216, #394, #417                                     |                            |
| cli.py:703                        | _travail_du_tour         | _turn_work               | 3    | #216, #394, #417                                     |                            |
| cli.py:740                        | _tour_ecrit              | _turn_written            | 4    | #216, #394, #417                                     |                            |
| cli.py:752                        | _attente                 | _pending                 | 3    | #216, #394, #417                                     |                            |
| cli.py:757                        | reste                    | remaining                | 33   | #216, #394, #417                                     |                            |
| exploitation.py:696               | humain                   | human                    | 14   | #194, #216, #326, #394, #420, #424                   | nested predicate           |
| exploitation.py:1078              | politique                | policy                   | 6    | #194, #216, #326, #394, #420, #424                   | nested memoised lookup     |
| grand_livre.py:264                | _claude_cumul            | _claude_cumulative       | 2    | —                                                    |                            |
| grand_livre.py:306                | _codex_cumul             | _codex_cumulative        | 2    | —                                                    |                            |
| identity.py:179                   | chantier_of              | workstream_of            | 10   | —                                                    |                            |
| mail.py:207                       | _tronque                 | _truncate                | 2    | #216, #394                                           |                            |
| menage.py:1104                    | ouvrir                   | open_path                | 3    | —                                                    | nested chmod helper        |
| menage.py:2505                    | cmd_menage               | cmd_housekeeping         | 5    | —                                                    |                            |
| mesh_cli.py:555                   | _host_menage             | _host_housekeeping       | 2    | #194, #326, #412, #417, #420                         |                            |
| mesh_cli.py:583                   | _print_host_menage       | _print_host_housekeeping | 3    | #194, #326, #412, #417, #420                         |                            |
| mesh_cli.py:649                   | _print_host_fiches       | _print_host_cards        | 3    | #194, #326, #412, #417, #420                         |                            |
| mesh_cli.py:1561                  | _etat_chemin             | _state_path              | 3    | #194, #326, #412, #417, #420                         |                            |
| mesh_cli.py:1565                  | _lit_etat                | _read_state              | 4    | #194, #326, #412, #417, #420                         |                            |
| mesh_cli.py:1573                  | _ecrit_etat              | _write_state             | 2    | #194, #326, #412, #417, #420                         |                            |
| plan_github.py:357                | fiche_link               | card_link                | 5    | —                                                    |                            |
| progress.py:142                   | _jalons_de_lot           | _work_item_milestones    | 14   | —                                                    |                            |
| progress.py:265                   | _jalons_declares         | _declared_milestones     | 2    | —                                                    |                            |
| reprise.py:58                     | RepriseError             | ResumeError              | 35   | #377, #424                                           |                            |
| reprise.py:306                    | _fil                     | _thread                  | 16   | #377, #424                                           |                            |
| roadmap_dev.py:46                 | jalon                    | milestone                | 11   | —                                                    |                            |
| roadmap_dev.py:51                 | livree                   | delivered                | 7    | —                                                    |                            |
| roadmap_dev.py:55                 | abandonnee               | abandoned                | 5    | —                                                    |                            |
| roadmap_dev.py:59                 | en_cours                 | in_progress              | 9    | —                                                    |                            |
| roadmap_dev.py:67                 | etat_generique           | generic_state            | 3    | —                                                    |                            |
| runner.py:57                      | _duree                   | _duration                | 5    | #194, #216, #394, #417, #420, #421, #422, #423, #424 |                            |
| runner.py:171                     | _ecrire_sans_attendre    | _write_without_waiting   | 2    | #194, #216, #394, #417, #420, #421, #422, #423, #424 |                            |
| runner.py:189                     | _sortie_sure             | _safe_exit               | 5    | #194, #216, #394, #417, #420, #421, #422, #423, #424 |                            |
| runner.py:212                     | _LancementImpossible     | _LaunchImpossible        | 5    | #194, #216, #394, #417, #420, #421, #422, #423, #424 |                            |
| runner.py:222                     | Reprise                  | Backoff                  | 10   | #194, #216, #394, #417, #420, #421, #422, #423, #424 | retry backoff, not resume  |
| runner.py:239                     | en_panne                 | failed                   | 7    | #194, #216, #394, #417, #420, #421, #422, #423, #424 | Reprise property           |
| runner.py:242                     | echec                    | failure                  | 19   | #194, #216, #394, #417, #420, #421, #422, #423, #424 | Reprise method             |
| runner.py:249                     | duree                    | duration                 | 18   | #194, #216, #394, #417, #420, #421, #422, #423, #424 | Reprise method             |
| runner.py:252                     | retablie                 | recovered                | 7    | #194, #216, #394, #417, #420, #421, #422, #423, #424 | Reprise method             |
| runner.py:524                     | _fil_note                | _thread_note             | 29   | #194, #216, #394, #417, #420, #421, #422, #423, #424 |                            |
| runner.py:1658                    | _panne_de_base           | _database_failure        | 3    | #194, #216, #394, #417, #420, #421, #422, #423, #424 |                            |
| runner.py:1685                    | _base_retablie           | _base_restored           | 2    | #194, #216, #394, #417, #420, #421, #422, #423, #424 |                            |
| runner.py:1691                    | _avec_reprise            | _with_resume             | 19   | #194, #216, #394, #417, #420, #421, #422, #423, #424 |                            |
| runner.py:1721                    | _restaure_consigne       | _restore_instruction     | 2    | #194, #216, #394, #417, #420, #421, #422, #423, #424 |                            |
| runner.py:1739                    | _enregistre_session      | _record_session          | 7    | #194, #216, #394, #417, #420, #421, #422, #423, #424 |                            |
| runner.py:1752                    | _ecarte_session_fille    | _discard_child_session   | 2    | #194, #216, #394, #417, #420, #421, #422, #423, #424 |                            |
| runner.py:1761                    | _ecarte_session_inconnue | _discard_unknown_session | 2    | #194, #216, #394, #417, #420, #421, #422, #423, #424 |                            |
| runner.py:1771                    | _ecarte_session          | _discard_session         | 3    | #194, #216, #394, #417, #420, #421, #422, #423, #424 |                            |
| runner.py:1821                    | _session_connue          | _session_known           | 3    | #194, #216, #394, #417, #420, #421, #422, #423, #424 |                            |
| runner.py:1860                    | connue                   | known                    | 17   | #194, #216, #394, #417, #420, #421, #422, #423, #424 | nested predicate           |
| runner.py:1878                    | _avertit_session         | _warn_session            | 5    | #194, #216, #394, #417, #420, #421, #422, #423, #424 |                            |
| runner.py:1980                    | _echec_d_hote            | _host_failure            | 2    | #194, #216, #394, #417, #420, #421, #422, #423, #424 |                            |
| runner.py:2343                    | _leve_pause_budget       | _clear_budget_pause      | 2    | #194, #216, #394, #417, #420, #421, #422, #423, #424 |                            |
| runner.py:2393                    | _compta_en_attente       | _accounting_pending      | 4    | #194, #216, #394, #417, #420, #421, #422, #423, #424 |                            |
| runner.py:2410                    | _compta_marque           | _accounting_mark         | 7    | #194, #216, #394, #417, #420, #421, #422, #423, #424 |                            |
| runner.py:2429                    | _compta_ecrit            | _accounting_write        | 3    | #194, #216, #394, #417, #420, #421, #422, #423, #424 |                            |
| runner.py:2485                    | _compta_repare           | _accounting_repair       | 4    | #194, #216, #394, #417, #420, #421, #422, #423, #424 |                            |
| runner.py:2502                    | _annonce_des_evenements  | _announce_events         | 4    | #194, #216, #394, #417, #420, #421, #422, #423, #424 |                            |
| runner.py:2529                    | _compta_annonce          | _accounting_announce     | 2    | #194, #216, #394, #417, #420, #421, #422, #423, #424 |                            |
| runner.py:2547                    | _compta_termine          | _accounting_done         | 6    | #194, #216, #394, #417, #420, #421, #422, #423, #424 |                            |
| runner.py:3047                    | _budget_courrier         | _mail_budget             | 3    | #194, #216, #394, #417, #420, #421, #422, #423, #424 |                            |
| runner.py:3059                    | _courrier_spec           | _mail_spec               | 3    | #194, #216, #394, #417, #420, #421, #422, #423, #424 |                            |
| runner.py:3164                    | _livre_courrier          | _deliver_mail            | 2    | #194, #216, #394, #417, #420, #421, #422, #423, #424 |                            |
| runner.py:3254                    | _menage_apres_tour       | _housekeeping_after_turn | 2    | #194, #216, #394, #417, #420, #421, #422, #423, #424 |                            |
| runner.py:4007                    | _note_courrier_borne     | _mail_note_bounded       | 3    | #194, #216, #394, #417, #420, #421, #422, #423, #424 |                            |
| runner.py:6512                    | battement                | heartbeat                | 2    | #194, #216, #394, #417, #420, #421, #422, #423, #424 |                            |
| runner.py:6526                    | veilleur                 | watchdog                 | 2    | #194, #216, #394, #417, #420, #421, #422, #423, #424 |                            |
| storage/postgres/mailbox.py:201   | _bail_sql                | _lease_sql               | 3    | —                                                    |                            |
| storage/postgres/mailbox.py:215   | _bail_params             | _lease_params            | 3    | —                                                    |                            |
| storage/postgres/operations.py:30 | _sans_compte_nul         | _without_null_account    | 3    | #420, #422                                           |                            |

**Dev scripts — 28 names** (28):

| file                    | French name         | English proposal | refs | open PRs |
|-------------------------|---------------------|------------------|------|----------|
| scripts/carte.py        | _par_module         | _per_module      | 5    | —        |
| scripts/carte.py        | chemin_relatif      | relative_path    | 4    | —        |
| scripts/carte.py        | ecrire              | write            | 13   | —        |
| scripts/carte.py        | ecrire_rc           | write_rc         | 2    | —        |
| scripts/carte.py        | fusionner           | merge            | 4    | —        |
| scripts/carte.py        | lire                | read             | 31   | —        |
| scripts/carte.py        | module_du_contexte  | context_module   | 10   | —        |
| scripts/carte.py        | montrer             | show             | 3    | —        |
| scripts/carte.py        | tables_de_migration | migration_tables | 4    | —        |
| scripts/carte_mesure.py | chemin_du_depot     | repository_path  | 4    | —        |
| scripts/ci-flux.py      | _duree              | _duration        | 5    | —        |
| scripts/ci-flux.py      | _ligne_echec        | _failure_line    | 2    | —        |
| scripts/ci-flux.py      | conclusion_de       | conclusion_of    | 3    | —        |
| scripts/ci-flux.py      | evenements          | events           | 34   | —        |
| scripts/ci-flux.py      | lire                | read             | 31   | —        |
| scripts/ci-flux.py      | maj                 | update           | 4    | —        |
| scripts/ci-flux.py      | sortie              | output           | 147  | —        |
| scripts/test-parts.py   | _Mesure             | _Measure         | 4    | —        |
| scripts/test-parts.py   | _module_de          | _module_of       | 6    | —        |
| scripts/test-parts.py   | _plan_pilote        | _driver_plan     | 3    | —        |
| scripts/test-parts.py   | durees              | durations        | 13   | —        |
| scripts/test-parts.py   | durees_de_la_carte  | map_durations    | 4    | —        |
| scripts/test-parts.py   | ecrire              | write            | 13   | —        |
| scripts/test-parts.py   | fichiers_changes    | changed_files    | 3    | —        |
| scripts/test-parts.py   | lire_liste          | read_list        | 8    | —        |
| scripts/test-parts.py   | lire_plan           | read_plan        | 3    | —        |
| scripts/test-parts.py   | nombre_de_tests     | test_count       | 3    | —        |
| scripts/test-parts.py   | noms_ambigus        | ambiguous_names  | 4    | —        |

**Tests — 2 090 names in 155 files.** They are almost all `test_<French sentence>` names, plus
French helper classes (`MenageWiringTests`, `Reprise389Test`, `JalonsTest`, `DepenseReelleTest`…)
and a few helpers (`lire`, `ecrire`, `jamais`, `en_panne`, `_fil`). Renaming them is mechanical
but touches every test file and therefore every open PR; stage 5 proposes how to do it without
blocking production work, and asks mesh-design whether it is in scope at all.

### 2.3 Database: tables, columns, other objects

- **Tables: 0 French** among the 64 tables created by migrations.
- **Columns:** 1 French column outside the kept domain term, plus 5 `lot` columns:

| object                  | suggested            | comment                                                                                                                                                   |
|-------------------------|----------------------|-----------------------------------------------------------------------------------------------------------------------------------------------------------|
| agent_registry.chantier | workstream           | 1 column; read/written by backend.py, fil.py, identity.py, registry.py, mesh_cli.py, storage/postgres/*                                                   |
| conversation_log.lot    | work_item (decision) | domain term kept by mesh-design; 5 columns: conversation_log.lot, housekeeping_log.lot, managed_worktrees.lot, thread_index.lot, work_resolutions.lot_tip |

- **Other objects:** all function, trigger and view names are English. Index names are English
  except `ci_runs_lot_idx`, which uses the kept term `lot`.

### 2.4 Migration file names (applied — never renamed)

14 of the 68 migration file names are French. `src/ameesh/migrations/*.sql` are applied
migrations: **their file names and contents are never renamed** (rule repeated in the lot).
A new migration that renames a DB object is a **new file with an English name**.

| applied migration (keep)         |
|----------------------------------|
| 0029_ressources_placement        |
| 0030_mode_et_arret               |
| 0031_delegation_a_echeance       |
| 0032_plusieurs_canons            |
| 0034_liaisons_de_session         |
| 0035_authentificateurs_par_canon |
| 0036_corrections_de_relecture    |
| 0041_plafond_de_contexte         |
| 0042_budget_en_base              |
| 0043_grand_livre_corrige         |
| 0044_feuille_de_route            |
| 0045_menage                      |
| 0046_plafonds_du_tour            |
| 0047_alimentation_hote           |

Reference counts are 1 each (the file itself); none is referenced by name in code, tests or
docs outside `schema_migrations` rows.

### 2.5 CLI options, commands and positionals

**Options — 27** (English alias already present in brackets):

| current         | suggested                          | refs | defined in                | open PRs on those files                                    |
|-----------------|------------------------------------|------|---------------------------|------------------------------------------------------------|
| --chantier      | --workstream                       | 5    | runner.py, reprise.py (2) | #194, #216, #377, #394, #417, #420, #421, #422, #423, #424 |
| --consigne      | --instruction                      | 6    | chat.py                   | —                                                          |
| --debut         | --start                            | 11   | plan_cli.py               | —                                                          |
| --fin           | --end                              | 10   | plan_cli.py               | —                                                          |
| --livraison     | --delivery                         | 9    | plan_cli.py               | —                                                          |
| --deploiement   | drop (unused alias of --livraison) | 1    | plan_cli.py               | —                                                          |
| --depend-de     | --depends-on                       | 4    | roadmap.py                | —                                                          |
| --externe       | --external                         | 18   | mesh_cli.py (2)           | #194, #326, #412, #417, #420                               |
| --fiche         | --card                             | 2    | roadmap.py                | —                                                          |
| --organisation  | keep (British spelling, §5.4)      | 2    | serve.py                  | —                                                          |
| --porteur       | --owner                            | 2    | roadmap.py                | —                                                          |
| --pour          | --due                              | 14   | roadmap.py (2)            | —                                                          |
| --projet        | --project                          | 4    | roadmap.py                | —                                                          |
| --tache         | drop (unused alias of --lot)       | 1    | roadmap.py                | —                                                          |
| --carte         | --map                              | 5    | scripts/test-parts.py     | —                                                          |
| --liste         | --list                             | 13   | scripts/test-parts.py     | —                                                          |
| --pilote        | --driver                           | 12   | scripts/test-parts.py (2) | —                                                          |
| --durees-sortie | --output-durations                 | 5    | scripts/test-parts.py     | —                                                          |
| --depuis        | --since                            | 7    | scripts/test-parts.py     | —                                                          |
| --flux          | --flow                             | 13   | scripts/test-parts.py     | —                                                          |
| --etiquette     | --label                            | 2    | scripts/test-parts.py     | —                                                          |
| --mesure        | --measure                          | 7    | scripts/test-parts.py     | —                                                          |
| --nom           | --name                             | 5    | scripts/ci-flux.py        | —                                                          |
| --intervalle    | --interval                         | 1    | scripts/ci-flux.py        | —                                                          |
| --duree-max     | --max-duration                     | 1    | scripts/ci-flux.py        | —                                                          |
| --sortie        | --output                           | 3    | scripts/carte.py          | —                                                          |
| --precedente    | --previous                         | 3    | scripts/carte.py          | —                                                          |

**Subcommands — 11** (reference counts are omitted: `menage`, `resume`, `blocs`, `sortie` etc.
collide with the module name or an English word, so a count would be meaningless):

| current             | suggested                           | defined in                         | open PRs                     |
|---------------------|-------------------------------------|------------------------------------|------------------------------|
| ameesh menage       | ameesh housekeeping (alias: menage) | mesh_cli.py                        | #194, #326, #412, #417, #420 |
| carte installer     | map install                         | scripts/carte.py                   | —                            |
| carte fusionner     | map merge                           | scripts/carte.py                   | —                            |
| carte montrer       | map show                            | scripts/carte.py                   | —                            |
| carte blocs         | map blocks                          | scripts/carte.py                   | —                            |
| ci-flux publier     | ci-flow publish                     | scripts/ci-flux.py                 | —                            |
| ci-flux fermer      | ci-flow close                       | scripts/ci-flux.py                 | —                            |
| ci-flux resume      | ci-flow summary                     | scripts/ci-flux.py (means summary) | —                            |
| test-parts repartir | test-parts split                    | scripts/test-parts.py              | —                            |
| test-parts lancer   | test-parts run                      | scripts/test-parts.py              | —                            |
| test-parts durees   | test-parts durations                | scripts/test-parts.py              | —                            |

**Positional names — 6** (metavar/attribute names; no user-visible compatibility needed):

| current     | suggested   | defined in                                |
|-------------|-------------|-------------------------------------------|
| identifiant | identifier  | harness_cli.py                            |
| carte       | map         | scripts/carte.py                          |
| cles        | keys        | scripts/carte.py                          |
| dossiers    | directories | scripts/carte.py                          |
| fichier     | file        | scripts/carte.py, scripts/ci-flux.py      |
| fichiers    | files       | scripts/test-parts.py, scripts/ci-flux.py |

`--lot`, `--new-lot`, `--stale-lot`, `--orphan-lot`, `--parent-lot`, `--take-lot-*` and the
`lot` positional use the kept domain term and are **out of scope**.

### 2.6 Configuration keys and canon model fields

- **No French configuration-file key.** `Config` in `src/ameesh/config.py` reads only the
  dataclass field names from `~/.config/ameesh/config.json` (unknown keys are ignored), all
  English. `ApprovalConfig` (approve/config.py) is English too. No French `AMEESH_*` env var.
- **Canon model fields:**

| current                   | suggested              | comment                                                                                                                                    |
|---------------------------|------------------------|--------------------------------------------------------------------------------------------------------------------------------------------|
| canon model field `fiche` | card                   | 6 dataclass fields in canon.py: Member, Agent, Host, Placement, WorkPackage, DecisionNote; the canon frontmatter itself has no `fiche` key |
| `organisation`            | keep (British spelling, §5.4) | British spelling, also an OpenAPI field and --organisation; not French-only                                                                |

### 2.7 Adjacent French identifiers: JSON/API keys and stored values

These are not in the six categories of the request but they are code identifiers and they are
the most compatibility-sensitive part (the progress page and the v1 API read them):

| key                                                        | refs | where                                                                                        |
|------------------------------------------------------------|------|----------------------------------------------------------------------------------------------|
| chantier                                                   | 14   | src/ameesh/backend.py:366, plan_github.py:955, tests; `ameesh list/hosts` JSON               |
| externe                                                    | 13   | work.py, registry.py, projects.py, mesh_cli.py, reprise.py, tests; `mode` value and JSON key |
| etat                                                       | 6    | runner.py trace meta (rotation), tests/test_fil.py fixtures                                  |
| durees                                                     | 6    | scripts/carte.py, scripts/test-parts.py, tests/test_l161f_plan.py                            |
| reprise                                                    | 5    | harness descriptor key (`reprise` argv) and runner thread meta                               |
| debut                                                      | 5    | roadmap.py `real` object, plan_cli dest, tests                                               |
| flux                                                       | 3    | scripts/test-parts.py result object, tests                                                   |
| organisation                                               | 2    | serve.py /.well-known/ameesh and /api/v1/me (kept, §5.4)                                     |
| mesure                                                     | 2    | scripts/carte.py result object                                                               |
| fil                                                        | 2    | fil.py thread trace meta                                                                     |
| projet / liste / menage / jalons / fin / consigne / livree | 7    | scattered JSON keys and test fixtures                                                        |

**Stored values** (set through config, CLI or stored in the database):

| vocabulary                     | current                                                     | suggested                                                 | where                                                                              |
|--------------------------------|-------------------------------------------------------------|-----------------------------------------------------------|------------------------------------------------------------------------------------|
| session_policy                 | par-lot, taille, jamais                                     | per-work-item, size, never                                | config.py SESSION_POLICIES, agent_registry.session_policy, `ameesh set`            |
| agent mode                     | execute, externe                                            | execute, external                                         | registry.py, work.py, projects.py, mesh_cli.py                                     |
| orphan / undeliverable reasons | inconnu, arrete, externe_sans_responsable, sans_tour        | unknown, stopped, external_without_owner, no_turn         | exploitation.py ORPHAN_REASONS, undeliverable.py REASONS                           |
| delegation reasons             | en_retard, rendu                                            | overdue, returned                                         | exploitation.py DELEGATION_REASONS                                                 |
| roadmap milestones             | demandee, debut, soumise, livree, abandonnee                | requested, in_progress, submitted, delivered, abandoned   | roadmap.py REAL_MILESTONES + LABELS (already translated on output)                 |
| turn kinds                     | consigne, messages, evenements, prioritaire, reprise, suite | instruction, mail, events, priority, resume, continuation | runner.py TURN_LABELS_EN (already translated on output; stored value stays French) |

## 3. Open PRs and conflicts

16 open PRs on 2026-10-12. The table lists the files that hold French identifiers and how many
open PRs touch them; `—` means the file is free and can be renamed without conflict.

| file                                      | open PRs touching it                                 | count |
|-------------------------------------------|------------------------------------------------------|-------|
| src/ameesh/runner.py                      | #194, #216, #394, #417, #420, #421, #422, #423, #424 | 9     |
| src/ameesh/exploitation.py                | #194, #216, #326, #394, #420, #424                   | 6     |
| src/ameesh/mesh_cli.py                    | #194, #326, #412, #417, #420                         | 5     |
| src/ameesh/background.py                  | #216, #394, #428                                     | 3     |
| src/ameesh/cli.py                         | #216, #394, #417                                     | 3     |
| src/ameesh/backend.py                     | #216, #394                                           | 2     |
| src/ameesh/canon.py                       | #216, #394                                           | 2     |
| src/ameesh/mail.py                        | #216, #394                                           | 2     |
| src/ameesh/reprise.py                     | #377, #424                                           | 2     |
| src/ameesh/storage/postgres/operations.py | #420, #422                                           | 2     |
| src/ameesh/adapters.py                    | #420                                                 | 1     |
| src/ameesh/fil.py                         | #421                                                 | 1     |
| src/ameesh/sous_utilisation.py            | #424                                                 | 1     |
| src/ameesh/chat.py                        | —                                                    | 0     |
| src/ameesh/grand_livre.py                 | —                                                    | 0     |
| src/ameesh/identity.py                    | —                                                    | 0     |
| src/ameesh/menage.py                      | —                                                    | 0     |
| src/ameesh/plan_github.py                 | —                                                    | 0     |
| src/ameesh/progress.py                    | —                                                    | 0     |
| src/ameesh/roadmap_dev.py                 | —                                                    | 0     |
| src/ameesh/storage/postgres/mailbox.py    | —                                                    | 0     |

The hot spots are:

- `src/ameesh/runner.py` — 9 PRs (#194, #216, #394, #417, #420, #421, #422, #423, #424): 37 names.
- `src/ameesh/exploitation.py` — 6 PRs; `src/ameesh/mesh_cli.py` — 5 PRs; `src/ameesh/config.py` — 5 PRs.
- `src/ameesh/storage/interface.py` — 6 PRs (no French name left there, but any shared rename touches it).
- PRs **#216** and **#394** (stacked Path B work, 99/104 files) touch `canon.py`, `backend.py`,
  `mail.py`, `cli.py`, `main.py`, `mesh_cli.py`, `exploitation.py`, `config.py`, `runner.py` and
  `storage/interface.py`: assume conflict on any change to those files until they land.

Ordering rules used by the plan:

1. **Never touch a file that an open PR touches** if the rename can be done behind a shim or an
   alias from a free file instead.
2. **Module renames never touch importers**: the new module holds the code, the old module becomes
   a re-export shim, deleted later when its importers are quiet.
3. **Hot-file function renames wait** for the PRs on that file; `runner.py` is last.
4. **Applied migrations are immutable**: DB renames go through one new migration per rename,
   with a compatibility period (dual column + trigger, drop later).
5. **Every user-visible rename keeps the old spelling as an alias** for at least one release:
   option alias, subcommand alias, accepted config/DB value, duplicate JSON key.

## 4. Plan: ordered small PRs

Each stage is one or more small PRs. "Free" means no open PR touches the file. All PRs carry
`ameesh-work: 147` (L148); this document's own PR carries `ameesh-work: 469`.

### Stage 0 — deprecate the unused French aliases (one PR, free files)

- `--tache` (alias of `--lot`) and `--deploiement` (alias of `--livraison`) have 0 references
  in code, tests, docs and scripts, but removing an option string is a breaking change for
  `scripts/version-level.py`: the next version would have to be 2.0.0. They stay accepted,
  hidden from `--help`, and print one line on stderr naming the canonical spelling
  (`--tache is deprecated, use --lot`), through `src/ameesh/deprecated.py` (`DeprecatedAlias`).
- Keep `--lot` (kept term). No other change. The actual removal moves to stage 7, with the
  next major version.
- Files: `src/ameesh/roadmap.py`, `src/ameesh/plan_cli.py`, `src/ameesh/deprecated.py`.
  Open PRs: none. Implemented by #446 (`ameesh-work: 147`).

### Stage 1 — production module renames behind shims

**1a — free now (2 PRs).** One PR per module. The new module contains the moved code; the old
module keeps only a re-export shim, so no importer is touched in this stage:

| PR   | free file      | new module      | shim kept                         | also renames (inside the file)                                       |
|------|----------------|-----------------|-----------------------------------|----------------------------------------------------------------------|
| 1a-1 | grand_livre.py | ledger.py       | grand_livre.py                    | _claude_cumul → _claude_cumulative, _codex_cumul → _codex_cumulative |
| 1a-2 | menage.py      | housekeeping.py | menage.py + `ameesh menage` alias | cmd_menage → cmd_housekeeping, ouvrir → open_path                    |

**1b — gated on three small PRs.** The same recipe applies to the three remaining modules, but
open PRs touch their files, so each starts only after its PR merges (re-check `gh pr list`):

| PR   | file                | new module    | wait for                        |
|------|---------------------|---------------|---------------------------------|
| 1b-1 | sous_utilisation.py | underuse.py   | #424 (critical roles)           |
| 1b-2 | fil.py              | thread_log.py | #421 (thread expiry)            |
| 1b-3 | reprise.py          | resume.py     | #377 (no-bypass adopt) and #424 |

Shim recipe (module alias):

```python
"""Deprecated alias of ameesh.thread_log (L148)."""
from .thread_log import *          # noqa: F401,F403
from .thread_log import __all__    # if the module defines __all__
```

The shim is deleted by a later PR, once `grep -rn 'import fil\|from .fil\|fil\.' src tests`
is empty and no open PR touches the remaining importers. Same for the other four.

Also in stage 1a, in free files: `_bail_params`/`_bail_sql` → `_lease_params`/`_lease_sql`
(`storage/postgres/mailbox.py`). `adapters.py` (`hors_path`) is touched by #420 and moves to
stage 4.

### Stage 2 — CLI and free-file renames (3 PRs, no open-PR conflict)

**2a — free CLI files.** `plan_cli.py` (`--debut/--fin/--livraison`), `roadmap.py`
(`--pour`, `--projet`, `--porteur`, `--depend-de`, `--fiche`). `serve.py` (`--organisation`) is
out of this stage: decision §5.4 keeps the British spelling.
Recipe: English option first, `dest=` stays the current attribute name during the transition,
the French spelling gets its own hidden `DeprecatedAlias` argument (accepted, absent from
`--help`, one line on stderr naming the canonical spelling):

```python
p.add_argument("--start", dest="debut", default=None,
               help="planned start: YYYY-MM-DD, tomorrow, monday...; - clears")
p.add_argument("--debut", dest="debut", action=DeprecatedAlias, canonical="--start",
               default=argparse.SUPPRESS, help=argparse.SUPPRESS)
```

This is conflict-free even where tests still pass `--debut`, and the visible help becomes
English. A later PR swaps `dest` and the tests; the French strings leave in stage 7.

**2b — dev scripts.** `scripts/carte.py`, `scripts/carte_mesure.py`, `scripts/ci-flux.py`,
`scripts/test-parts.py`: 28 function names, 13 options, 11 subcommands, 6 positionals, 4 file
names (names in §2.1/§2.2/§2.5). Also update `.github/workflows/carte.yml`, `scripts/test.sh`,
`tests/parts/*` and `scripts/version-level.py` expectations (it parses `add_parser` calls).
Tests and CI call these scripts; do it in one PR so the workflow never sees a half-renamed tool.

**2c — free-file function renames.** `identity.py` (`chantier_of` → `workstream_of`),
`chat.py` (`consigne` → `instruction`) and `watch.py` (same name), `plan_github.py`
(`fiche_link` → `card_link`), `progress.py` (2 names), `roadmap_dev.py` (5 names).
`storage/postgres/mailbox.py` (2 names) was already done in stage 1a (§1a, free files).
Add aliases where the name is exported (`old = new`) and delete them in the cleanup PR.

### Stage 3 — database: `chantier` → `workstream` (2 migrations + 2 PRs)

`agent_registry.chantier` is written and read by `backend.py`, `fil.py`, `identity.py`,
`registry.py`, `projects.py`, `plan_github.py`, `stagnation.py`, `mesh_cli.py` and
`storage/postgres/*`; several of those are hot.
Zero-downtime recipe, no applied migration touched:

1. **Migration A (new, English name, e.g. `00xx_rename_chantier_workstream.sql`)** —
   `alter table agent_registry add column if not exists workstream text not null default '';`
   backfill from `chantier`, then a `before insert or update` trigger keeping both columns in
   sync in both directions (last write wins on the pair). Old code keeps working unchanged.
2. **PR B** — switch readers/writers to `workstream` in the free files first (`identity.py`,
   `projects.py`, `plan_github.py`, `stagnation.py`), and emit both `workstream` and `chantier`
   in the JSON listing (duplicate key, deprecated) so the progress page keeps working.
3. **PR C (later)** — switch the hot files when their PRs merge, stop emitting `chantier`, then
   **Migration D** drops the trigger and the `chantier` column after one release.

The `lot` columns are **not renamed** (kept term, see §0.2). If mesh-design reverses that
decision, the same recipe applies with `lot` → `work_item` and it must be scheduled with the
schema owner, because `work_items` already uses `work_item_id`.

### Stage 4 — hot files, after their open PRs merge (in this order)

| order | file                                      | names                                                                                                                           | open PRs to wait for (or coordinate with)      |
|-------|-------------------------------------------|---------------------------------------------------------------------------------------------------------------------------------|------------------------------------------------|
| 4a    | src/ameesh/adapters.py                    | 1 (hors_path)                                                                                                                   | #420                                           |
| 4b    | src/ameesh/reprise.py                     | 2 (RepriseError, _fil)                                                                                                          | #377, #424                                     |
| 4c    | src/ameesh/storage/postgres/operations.py | 1 (_sans_compte_nul)                                                                                                            | #420, #422                                     |
| 4d    | src/ameesh/mail.py + background.py        | 3 (_tronque, boucle, lignee)                                                                                                    | #394, #428                                     |
| 4e    | src/ameesh/backend.py                     | 1 (chantier)                                                                                                                    | #394                                           |
| 4f    | src/ameesh/cli.py                         | 11 (_Remise, _attente, _tour_ecrit, _travail_du_tour, abandonner, avis_borne, avis_borne_travail, lire, prendre, reste, solder) | #394, #417                                     |
| 4g    | src/ameesh/mesh_cli.py                    | 6 (+ `--externe`)                                                                                                               | #194, #326, #412, #417, #420                   |
| 4h    | src/ameesh/exploitation.py                | 2 (humain, politique)                                                                                                           | #194, #326, #394, #420, #424                   |
| 4i    | src/ameesh/canon.py                       | 5 (Fiche, _fiche, fiche, _read_fiches, host_fiches)                                                                             | #394                                           |
| 4j    | src/ameesh/runner.py                      | 37                                                                                                                              | #194, #394, #417, #420, #421, #422, #423, #424 |

`Fiche`/`fiche` need a public alias: `Card = Fiche` (and a `fiche` property on the canon
objects returning `card`) so canon consumers and the `to_dict` output keep working for one
release; `to_dict` may then emit both keys.

`runner.py` is the last file: 37 names, 8 open PRs, and the file also carries `Lancement`,
`Reprise`, `battement`, `veilleur` and the whole accounting vocabulary. One PR is enough; it
must be rebased on the day the last of #194/#394/#417/#420/#421/#422/#423/#424 merges.

### Stage 5 — tests (decision: (b) now)

2 090 French `test_*` names in 155 files. Options:

- **(a) In scope, file by file** — one PR per batch of ~10 files that no open PR touches, always
  from `origin/main`; mechanical rename plus the corresponding `pytest` node ids in the CI parts
  files and `scripts/carte.py` maps. It is safe but it is a large diff that collides with every
  test-writing PR for weeks.
- **(b) Out of scope** — keep French test names, which are prose-like and never leave the
  repository. Rename only the 22 French **test-file** names (§2.1) and the French helper
  classes/functions that are imported (`_fil`, `lire`, `ecrire`, `jamais`, `en_panne`…).
- **Decision (mesh-design, 2026-10-12 17:52 UTC): (b) now, (a) once the open-PR queue has
  drained** — the owner rule targets code identifiers, and the risk/benefit for `test_*`
  sentences is poor.

### Stage 6 — JSON/API keys and stored values

- Emit English keys (`workstream`, `external`, `state`, `durations`, `resume`, `start`,
  `flow`, `measure`, `thread`, `project`, `list`, `housekeeping`, `milestones`, `end`,
  `instruction`, `delivered`) **in addition to** the French ones for one release; drop the French
  keys in a later release. `docs/api/openapi-v1.json` must list both during the transition.
- Values: **deferred out of #147** (mesh-design decision, §5.5): `par-lot`, `en_retard`,
  `inconnu`, `externe` and the other stored vocabularies move to a separate later lot, which will
  accept both spellings on input, write the English one, migrate the rows in one migration
  (`session_policy`, `mode`, orphan/delegation reasons, roadmap milestones, turn kinds) and keep
  the existing `*_LABELS` translation tables as the read-side compatibility layer.

### Stage 7 — cleanup at the next major

- Delete the module shims, the `old = new` Python aliases, the CLI aliases and the duplicate
  JSON keys when the next major version (2.0.0) is prepared, with no open PR on the files:
  this stage holds those removals and nothing else.
- Re-run this inventory by script; the target is 0 French identifiers outside the kept `lot`
  vocabulary and the 14 applied migration file names.

## 5. Decisions (mesh-design, 2026-10-12)

`#147` is reassigned to `mty-amh-impl-04` (was `ameesh-dev7`); stages 0–2 start now, one small PR
per stage, each carrying `ameesh-work: 147`. Answers to the six questions this section used to
ask (mesh-design, 2026-10-12 17:52 UTC):

1. **Repositories**: `manaty/ameesh` only — `manaty/nexlink` is not covered by #147, so
   `porte_hote` stays out of scope.
2. **Tests (stage 5)**: option (b) now, option (a) once the open-PR queue has drained.
3. **`lot`**: the kept term applies to code identifiers too — the `lot` columns, `--lot`,
   `ThreadRef.lot` and `/api/v1/lots` are not renamed.
4. **`organisation`**: keep the British spelling; no API change for a spelling.
5. **Stored values**: out of #147 for now; stage 6's value migration becomes a separate lot.
6. **Start**: stages 0–2 now; stage 4 keeps waiting for the PRs that touch its files.

Follow-up (mesh-design, 2026-10-12 18:00 UTC): no stage may break anything. A renamed option
keeps its old spelling accepted, hidden from `--help`, with one line on stderr naming the
canonical spelling; `scripts/version-level.py` must keep seeing no breaking change (checked
with `scripts/version-level.py level`); shared-code renames go behind shims (module
re-exports, `old = new`, JSON keys emitted twice). Every removal is bundled into stage 7, at
the next major version.

## 6. Limits

- Local variables and instance attributes are not enumerated (e.g. `Reprise.echecs`,
  `_Remise.jeton`, `ouverte`, `solder`'s friends). They are French too and will be renamed
  with their functions when those files are handled; a full variable inventory is a different
  (much larger) job.
- French comments, docstrings and user-facing messages are out of scope by construction: the
  project convention is "code in English, docs and messages in French", and L156c already
  translated the CLI help/output.
- The reference counts for CLI options include tests and docs, so they measure the churn of a
  rename, not the number of definitions.
- Open-PR mapping is a snapshot of 2026-10-12 17:2x UTC; re-run `gh pr list` before starting a
  stage. The stage boundaries are chosen so a late-arriving PR only invalidates the stage it
  touches.
