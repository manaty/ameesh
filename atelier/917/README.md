# Atelier #917 — tête `93613d6b` : rebase sur `main 6eeecc8e` + témoin aligné sur L713

Atelier séparé, **aucune PR, aucun push de branche `lot/**`, aucun run, aucune
relance**. La branche `lot/917-stale-lot-equipe` (tête `dd5d6a67`) et la
PR #708 restent inchangées ; le gel, les avis et le feu existants ne sont ni
rejoués ni touchés.

## Objets

| objet | SHA |
|---|---|
| tête revue (#917, PR #708) | `dd5d6a67886eb3aa5ad2573a7c9d14cb207003b0` |
| `main` de base du rebase | `6eeecc8e914efb391c9e4be67058ae83519d8bd7` |
| commit lot rebasé | `cad1ce5a67ae88874c06b01819e1453e4f765af6` |
| arbre du commit lot (= union propre) | `0ddfd1545ea0a3d42b33cfa286460c6d0ae7b904` |
| **tête publiée** | `93613d6bb05fa9e58057596d4a5b9b521060afa5` |
| parent de la tête | `cad1ce5a67ae88874c06b01819e1453e4f765af6` |
| arbre de la tête | `8a1f6700b1b0760ea3193ecb9c6494f1c16f7ddc` |

## Ce que c'est

1. `cad1ce5a` = rebase de la tête revue `dd5d6a67` sur `main 6eeecc8e` :
   `git merge-tree --write-tree origin/main dd5d6a67` rend
   `0ddfd1545ea0a3d42b33cfa286460c6d0ae7b904`, exactement l'arbre de
   `cad1ce5a`. L'union est propre (rc=0), aucune retouche manuelle.
2. `93613d6b` = `cad1ce5a` + la correction du seul témoin
   `tests/test_l132_alert_briefs.py` : il attend `['orch-b']` (et non
   `['auditeur','orch-b']`) car L713 (`f11ab669`, dans `main`) exclut
   l'auditeur des briefs `stale_lot`. Le commentaire du témoin cite #917 et
   L713.

Le conflit sémantique #917 × #713 est donc résolu par l'alignement du témoin,
pas par un changement de production (voir constance ci-dessous).

## Constance production/SQL

- L'arbre du commit lot `cad1ce5a` est **bit à bit** l'arbre de l'union propre
  `main × dd5d6a67` : `0ddfd154…`. Aucune production ni SQL retouché.
- La correction `93613d6b` n'écarte cet arbre que par
  `tests/test_l132_alert_briefs.py` (1 fichier, +3/−2) :
  `git diff --name-status 0ddfd154… 93613d6b`.
- Delta du lot vs `main` : 7 fichiers, +36/−7, **aucun `.sql`, aucune
  migration** (`constance-production.txt` §3).
- Depuis la base du lot, `main` a modifié deux fichiers de production du lot
  (#713 `f11ab669`, #1037 `bf1a0913`) : l'union les incorpore sans conflit ;
  ce sont des contenus déjà fusionnés et revus dans `main`.
- Reproduire : `constance-production.txt` (commandes et sorties brutes).

## Interaction `stale_lot` × L713 (vérifiée sur l'arbre publié)

Matrice rejouée sur `93613d6b` (listing type : équipes `a`, `b`, `ameesh` ;
orchestrateurs portés par l'alerte, dont `auditeur` équipe `ameesh`) :

| cas | destinataires |
|---|---|
| assigné connu, équipe `a` | `orch-a` (+ sans-équipe) |
| assigné connu, équipe `ameesh` | `mesh-design`, `auditeur` (+ sans-équipe) |
| assigné inconnu, `lot_team=b` | `orch-b` (+ sans-équipe) |
| assigné inconnu, `lot_team=ameesh` | `mesh-design`, `auditeur` (+ sans-équipe) |
| assigné inconnu, sans `lot_team` | tous les orchestrateurs portés par l'alerte |
| canal auditeur seul (auditeur absent des orchestrateurs) | **jamais** ajouté pour `stale_lot` ; ajouté pour `idle_capacity` |

Lecture : le canal auditeur est fermé pour `stale_lot` dans tous les cas
(assigné connu ou inconnu) — c'est la règle L713. L'auditeur peut encore
recevoir un `stale_lot` **par le canal orchestrateur** s'il figure dans
`alert.orchestrators` et que son équipe correspond (ou est vide) : sur la base
live du 14/10 10:5xZ, `auditeur` y figure par l'historique d'attribution 30 j
(équipe `ameesh`), donc les lots ameesh le joignent encore. C'est le sujet de
**#1114** (éligibilité par historique), évalué à son propre stade ; ce n'est
pas un écart de cette adaptation.

## Preuves (auteur, ciblées)

- `tests.test_l132_alert_briefs` + `tests.test_l26_exploitation` :
  **56 OK** (10 + 46, pilote psql, banc local) sur l'arbre `93613d6b`.
- Aucune suite complète locale (canon §3) ; aucun run CI déclenché par cette
  publication d'atelier.

## Récupération

```sh
git fetch origin '+refs/atelier/917/*:refs/atelier/917/*'
git show refs/atelier/917/tete-93613d6b              # la tête publiée
git ls-tree -r refs/atelier/917/enveloppe-93613d6b   # cette enveloppe
```

Refs poussées : `refs/atelier/917/tete-93613d6b` (→ `93613d6b`) et
`refs/atelier/917/enveloppe-93613d6b` (→ ce commit, sans parent).

## Patchs

1. `correction-tests.patch` — `git diff cad1ce5a 93613d6b` : la correction,
   témoin seul (18 lignes).
2. `lot-delta-vs-main.patch` — `git diff main 93613d6b` : le contenu final du
   lot (7 fichiers, +36/−7).
