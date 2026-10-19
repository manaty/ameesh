# Paquet integration/paquet-h-20261019-0400 — composition (§12)

Base : `main` **`8d9c067c05d6`** (paquet h-0100, PR #1946, fusionné en
`78491d524`). Branche `integration/paquet-h-20261019-0400`, cible `main`.
Une seule suite complète est demandée sur la tête de ce manifeste.

Demandes : auditeur #111513 (03:4x) — composer le paquet horaire h-0400 sur
`main 8d9c067c`, à partir de la liste calculée, écarts motivés au manifeste ;
#2597 (incident P1) garde sa propre CI et sa fusion hors coupure (§11 bis).

## Admissions (7 unités, 7 lots portés)

- #2198
- #2505
- #2527
- #2580
- #2592
- #2560
- #2591

## Écartés (ce véhicule)

| lot | motif |
|---|---|
| #2597 | incident P1 CI main : sa CI et sa fusion restent hors coupure (§11 bis, PR #1963), gel `dfb5ff412d55` |
| #2498 | verdicts-missing : un seul avis (ameesh-cx2) sur le gel `18c1c622f1bf` ; le gel auteur est demandé à dev2 — prêt au prochain paquet |

## Composition (7 unités)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #2198 | `e91a0254f6f4` (tête gelée) | `2a7766db7abb` | sensible — vm.sh drain : mineurs de la relecture #2184 (N1-N5) |
| 2 | #2505 | `7e058770cc8b` (tête gelée) | `56b016d6e45a` | sensible — `fire.ready_verdicts` : la classe se lit contre `origin/<cible>` |
| 3 | #2527 | `854953bca8f0` (tête gelée) | `97f429d0f1fb` | sensible — `turn_lot` : un événement informatif (prise automatique L165) ne dédie plus la session |
| 4 | #2580 | `b4a73a9df70a` (tête gelée) | `37350483afc1` | sensible — contrôle de partage : un seul registre d'hôte, celui de #313 |
| 5 | #2592 | `944ca11d3f7f` (tête gelée) | `9123354a2967` | sensible — gel empilé sur #2580 (delta 1 commit) : identité de mesh stable |
| 6 | #2560 | `7d5437d4cfde` (tête gelée) | `5ca761b7ff8e` | léger — `backlog.py` : marqueur de revue via `review_lot.review_ref` |
| 7 | #2591 | `454267cda2e2` (tête gelée) | `771bf875d287` | léger — tests #2525 : `AMEESH_SYSTEM_TMP` absente, pas vide |

Chaque commit de composition a le lot en second parent, son sujet nomme le lot
et son SHA, et il porte un trailer `ameesh-work: <lot>`. #2592 est empilé sur
#2580 : sa tête contient le socle de #2580, composé avant elle ; la fusion
n'ajoute que son delta propre. Aucun conflit de composition n'a été rencontré,
aucune résolution ni adaptation.

## Contrôles

- Liste calculée `scripts/paquet-candidats.py` à 03:47 (Europe/Paris), base
  `8d9c067c`: **8 lots prêts, 19 en attente, 0 lecture en erreur**. Les 7 prêts
  hors incident sont composés ; #2597 est écarté (hors coupure) et #2498
  (non prêt) est écarté avec motif.
- `paquet-inclusion` (tête `f36f69baa325` + ce manifeste) : **rc 0** — 7 lots
  attendus, 7 entrées de composition (7 compositions, 0 retrait), 6 contenus
  présents, 1 partiel, aucun défaut. Le partiel #2580 (`docs/COMPTES.md`,
  `src/ameesh/account_claims.py`, `tests/test_2580_partage_comptes.py`) est le
  socle remplacé par le delta empilé de #2592 (identité de mesh stable) : ce
  n'est pas une perte.
- `git diff --check` vert ; aucune migration (24 fichiers, +1625/−270 hors
  manifeste).
- Suite complète unique demandée sur la tête de ce manifeste
  (`ci-full-<sha>`), avec la revue unitaire du paquet pour seul avis de la
  demande de CI (§12, `--full --avis 0`).
