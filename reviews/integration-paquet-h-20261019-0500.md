# Paquet integration/paquet-h-20261019-0500 — composition (§12)

Base : `main` **`f70ce01b61aa`** (paquet h-0400, PR #1970, fusionné en
`f70ce01b6`). Branche `integration/paquet-h-20261019-0500`, cible `main`.
Une seule suite complète est demandée sur la tête de ce manifeste.

Demandes : composeur, paquet horaire §11 bis — main a bougé à `f70ce01b`
(h-0400 fusionné, paquet h-0500 dû) ; liste calculée `paquet-candidats.py`.
Entrées de préparation : message `ameesh-dev3` #112035 (compatibilité #2600 ×
main, × remise #2466, × #2613), message `mty-amh-impl-02` du 03:03 (#2627).

## Admissions (11 unités, 11 lots portés)

- #2481
- #2535
- #2551
- #2561
- #2584
- #2593
- #2598
- #2599
- #2600
- #2610
- #2627

## Écartés (ce véhicule)

| lot | motif |
|---|---|
| #2466 | conflit avec `main` sur le gel `abc2fbf796a5` (`docs/EXECUTEUR-MEDIEE.md`, `src/ameesh/runner.py`) ; la remise `e6115166` a été testée par dev3 mais conflit avec #2481 dans ce véhicule (`src/ameesh/storage/postgres/work.py`) — une résolution fraîche est requise, prochain paquet |
| #2613 | verdicts-missing : un seul avis (ameesh-cx1) sur `0fe93df279a4` ; second verdict d'une autre famille attendu |
| #2601 | no-verdict : aucun relecteur désigné sur `b7bf32708dba` |
| #2451 | verdicts-missing : aucun avis sur `284a04d226c5` |

Les 24 autres lots en attente le restent (verdicts-missing, no-verdict,
conflit avec `main`) : aucun n'est prêt à cette coupure.

## Composition (11 unités)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #2481 | `2b37a90245bc` (tête gelée) | `3f91ec369c6d` | sensible — work migration : une seule réservation par lot |
| 2 | #2535 | `0cb668efcda3` (tête gelée) | `6a636fbc1a23` | sensible — `--repo` explicite non checkout : refus nommé |
| 3 | #2551 | `e13237c550f8` (tête gelée) | `1b2fdd0b00f6` | sensible — SLO `intake_started` : le délai court pendant une dépendance bloquante |
| 4 | #2561 | `73999caa078a` (tête gelée) | `33d69993806c` | sensible — désignation automatique des relecteurs : échecs au gel |
| 5 | #2584 | `3f85f8d3944f` (tête gelée) | `0b6dc6c5b5e4` | sensible — `backlog add` respecte le canon l.129 (panier par chantier) |
| 6 | #2593 | `ec3b872ea1ba` (tête gelée) | `875480335fc3` | léger — registre de partage : revendications non filtrées sur l'hôte |
| 7 | #2598 | `1a45d29e7590` (tête gelée) | `3d4bd2dd12cf` | léger — fenêtre de relecture JWKS : borne les échecs |
| 8 | #2599 | `8a43dccaf102` (tête gelée) | `febd2aa14a49` | léger — SLO/stagnation : lot porté par un paquet dont le porteur ne cite pas |
| 9 | #2600 | `43d936d584c0` (tête gelée) | `93a40166b8d5` | sensible — tests de bail du runner sur horloge simulée |
| 10 | #2610 | `50fd5d444d23` (tête gelée) | `bc96f04af961` | sensible — rotation de contexte : la suite de tour vide le `$TMPDIR` |
| 11 | #2627 | `47326c137d8d` (tête gelée) | `dc9469bf6a36` | léger — désignation au gel : compléter une désignation manuelle insuffisante |

Chaque commit de composition a le lot en second parent, son sujet nomme le lot
et son SHA, et il porte un trailer `ameesh-work: <lot>`. Aucun conflit de
composition rencontré, aucune résolution ni adaptation.

## Contrôles

- Liste calculée `scripts/paquet-candidats.py` à 03:11 (Europe/Paris), base
  `f70ce01b`: **11 lots prêts, 24 en attente, 0 lecture en erreur**
  (`--ready-out`). Les 11 prêts sont composés ; les écarts sont motivés
  ci-dessus.
- Matrice de conflits : 55 paires testées en `git merge-tree`, 0 conflit
  (hors #2466, écarté).
- `paquet-inclusion` (tête de ce manifeste, `--ready-file` + admissions) :
  **rc 0** — 11 lots attendus, 11 compositions, 0 défaut.
- `git diff --check` vert ; aucune migration SQL (43 fichiers, +4369/−535
  hors manifeste).
- Suite complète unique demandée sur la tête de ce manifeste
  (`ci-full-<sha>`), avec la revue unitaire du paquet pour seul avis de la
  demande de CI (§12, `--full --avis 0`).
