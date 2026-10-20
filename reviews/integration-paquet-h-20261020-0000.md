# Paquet integration/paquet-h-20261020-0000 — composition (§12)

Base : `main` **`66e993cd5d99`** (fusion du paquet h-2000, PR #2099, 19/10
23:31Z). Branche `integration/paquet-h-20261020-0000`, cible `main`. Une seule
suite complète est demandée sur la tête finale.

Demandes : composeur, paquet horaire §11 bis — signal de l'auditeur à 00:14Z
(impl-07, SLO paquet) : le paquet de 00:00Z n'est pas composé. Puis pilote
mesh-design (00:16Z, 00:33Z) : ajouter #2729, #2757 avec la résolution
`97155ecc` et #2443 avec la remise `6a587b2e` ; #2766 reste écarté (conflit,
`merge_queue.py`, docs-01 rebase). Liste calculée
`scripts/paquet-candidats.py` à 00:24Z sur `66e993cd5` : **7 prêts, 13 en
attente, 0 erreur** ; recalcul à 00:45Z après les ajouts : **11 prêts, 10 en
attente, 0 erreur**. Le paquet porte **9 unités, 9 lots**.

## Admissions (9 unités, 9 lots portés)

| lot | source | classe | avis | motif |
|---|---|---|---|---|
| #381 | `b68d7d8b45f5` (tête gelée) | light | cx1 (codex) + dev5 (DeepSeek) | chat : réponses finales et lecture, correctifs de relecture ; auteur dev1 |
| #2772 | `b51188430317` (tête gelée) | light | auditeur (Claude) | garde venv : les shims ne rouvrent pas le PATH de production ; auteur impl-08 |
| #2794 | `8db6b5b2adb5` (tête gelée) | sensitive | dev5 (DeepSeek) + cx1 (codex) | prévalidation : le plan nomme le script absent du dépôt candidat ; auteur dev2 |
| #2743 | `7940042bfd91` (tête gelée) | light | dev2 (Claude) | ménage des clones/worktrees ad hoc des lots finis ; auteur impl-04 |
| #2562 | `2ec7a16ea8a0` (tête gelée) | sensitive | dev7 (Claude) + impl-08 (DeepSeek) | SLO intake : compter depuis le début planifié ; auteur cx3 |
| #2538 | `e7834ed960b7` (tête gelée) | sensitive | dev1 (Claude) + cx3 (codex) | plan : lignes sans test depuis la base de la branche ; auteur dev3 |
| #2729 | `e78676845426` (tête gelée, gel #14001) | sensitive | auditeur (Claude) + cx1 (codex) | runner : drapeau `preempting` jamais remis à zéro (P1) ; auteur impl-04 |
| #2757 | résolution `97155ecc` | sensitive | dev5 (DeepSeek) + dev1 (Claude) sur `a605ff17` ; résolution mécanique, sans avis (§2) | alerte underused : perte projetée au rythme actuel ; auteur cx2 |
| #2443 | remise `6a587b2e` | sensitive | admission manuelle du pilote (note 20/10 01:5x) ; verdict dev4 (Claude) sur `4575e7fd` | paquet : lier chaque unité au paquet ; auteur impl-07 ; le `verdicts-missing` de `paquet-candidats` vient du défaut correcteur-auteur corrigé par #2739 |

## Composition (9 unités)

| unité | lot | source | commit | note |
|---|---|---|---|---|
| 1 | #381 | `b68d7d8b45f5` | `0cebe2793b33` | light ; 0 conflit |
| 2 | #2772 | `b51188430317` | `a4a9d60b5a0f` | light ; 0 conflit |
| 3 | #2794 | `8db6b5b2adb5` | `452cdbc4f0dd` | sensitive ; 0 conflit |
| 4 | #2743 | `7940042bfd91` | `bd9bf3329120` | light ; 0 conflit |
| 5 | #2562 | `2ec7a16ea8a0` | `31259935d6bb` | sensitive ; 0 conflit |
| 6 | #2538 | `e7834ed960b7` | `71c68c90dc2a` | sensitive ; 0 conflit |
| 7 | #2729 | `e78676845426` | `7f8f7441ce2e` | sensitive (P1) ; 0 conflit |
| 8 | #2757 | résolution `97155ecc` | `c799ad5eea6b` | sensitive ; 0 conflit |
| 9 | #2443 | remise `6a587b2e` | `46c8ee9061c6` | sensitive ; 0 conflit |

Chaque commit de composition a le lot en second parent, son sujet nomme le lot
et son SHA, et il porte un trailer `ameesh-work: <lot>`
(`scripts/paquet-compose.py --units --base 66e993cd5` : 9 unités de fusion,
9 lots portés).

## Résolutions de composition (2, préparées hors du paquet)

| paire | source | nature | preuve |
|---|---|---|---|
| #2757 × #2756 | `97155ecc` (`refs/ameesh/resolutions/2757/2756`) | résolution préparée, parents `a605ff17` (#2757) + `464d70ea` (h-1700, ancêtre de `main`) ; mécanique, sans avis (§2) ; fusion propre dans le paquet | merge de l'unité sans conflit, aucune résolution par le composeur |
| #2443 × main | `6a587b2e` (`refs/ameesh/resolutions/2443/main-66e993cd`) | remise préparée, parents `4575e7fd` (#2443) + `66e993cd` (main h-2000) ; admission manuelle du pilote ; fusion propre dans le paquet | merge de l'unité sans conflit, aucune résolution par le composeur |

Aucune résolution n'a été écrite par le composeur : les 9 unités se composent
proprement. **Une migration SQL est portée par #2443** :
`src/ameesh/migrations/0465_work_packet_members.sql`, additive (nouvelle table
interne `work_packet_members`), déjà relue sur `4575e7fd` ; aucune autre
migration n'est ajoutée ni modifiée par la composition.

## Écartés (ce véhicule)

| lot | motif |
|---|---|
| #2726 | **écarté sur la garde mécanique** : 6 constats sur sa tête `fd591f39` (`src/ameesh/lot_class.py` : littéraux `True`/`False` pour `reviews.distinct_families` et `reviews.exclude_author_family`) ; le dépôt livré doit être propre (`tests/test_2449_garde_mecanique.py`, `garde-mecanique.py` ok=false) ; à corriger ou exempter au registre, puis re-geler ; paquet suivant |
| #2786 | prêt à 00:30Z, non retenu par l'admission du pilote du 00:33Z (portée limitée aux trois lots nommés) ; hors de ce véhicule, au paquet suivant |
| #2609, #2755 | prêts au recalcul de 00:45Z, non retenus par l'admission du pilote du 00:33Z ; hors de ce véhicule, au paquet suivant |
| #2766 | conflit avec `main` (`src/ameesh/merge_queue.py`) ; docs-01 rebase (pilote 00:33Z) |
| #114, #2625, #2702, #2789 | `verdicts-missing` (un seul avis, deux familles requises pour un lot sensible) |
| #1515, #2614, #2771 | `blocked-verdict` / `no-verdict` : gel effectif refusé |

## Contrôles

- Liste calculée `scripts/paquet-candidats.py`, base `66e993cd5d99` : première
  lecture incomplète (2 erreurs de lecture d'objets, #2614 et #2786, clone
  partiel `blob:none`) ; les objets ont été lus depuis `origin` en lecture
  seule, puis la liste a été recalculée à 00:24Z : **7 prêts, 13 en attente,
  0 erreur** (`rc 0`). Recalcul à 00:45Z après les ajouts : **11 prêts,
  10 en attente, 0 erreur** (`rc 0`).
- Composition : **9 unités, 0 conflit** ; 56 fichiers, +4001/−209, hors
  manifeste ; aucun marqueur de conflit ; une migration additive (#2443,
  `0465_work_packet_members.sql`), aucune autre. `git diff
  --check` ne relève que deux lignes à blanc en fin de ligne dans
  `src/ameesh/ui/static/ameesh-ui.js` (bundle minifié de #381, déjà présent et
  identique sur la tête relue `b68d7d8b`) : rien d'introduit par la
  composition.
- `scripts/garde-mecanique.py --json` : **0 constat**, 1 exemption utilisée
  (#2642), 0 inutilisée.
- `scripts/doc-links.py` : 172 fichiers, **0 lien cassé**.
