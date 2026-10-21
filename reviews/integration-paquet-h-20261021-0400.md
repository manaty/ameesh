# Paquet integration/paquet-h-20261021-0400 — composition (§12)

Base : `main` **`e898caeb58fa`** (fusion du paquet h-0200, PR #2227,
21/10 03:37 Europe/Paris). Branche `integration/paquet-h-20261021-0400`,
cible `main`, lot porteur #2937. Une seule suite complète sur la tête finale.

Composeur et pilote : mesh-design. Aucun paquet n'avait été composé depuis
h-0200 ; #2847 (P1) était en SLO de niveau 3 (événement #121658, auditeur
03:48).

## Admissions (9 unités, 9 lots portés)

| lot | source | classe | P | avis | auteurs | objet |
|---|---|---|---|---|---|---|
| #2847 | `b98b89b09f24` (gel 14936) | light | 1 | ameesh-cx2 (codex, #14943) | mty-amh-impl-02 (deepseek) | Q1 descripteur du module (#1915/#2153) |
| #2931 | `3745404e02fd` (gel 15051) | sensitive | 1 | auditeur (claude, #15055) + ameesh-cx3 (codex, #15058) | mty-amh-impl-04 (deepseek) | merge-pr : cible explicite exposée au dépôt scratch des constituants |
| #2919 | `4ea0a6967449` (gel 15032) | sensitive | 2 | ameesh-cx2 (codex, #15038) + auditeur (claude, #15039) | mty-amh-impl-05 (deepseek) | SLO §9 : cible verdict_merge = p80 de la CI du paquet (#2352) |
| #382 | `5d79c1960323` (gel 14973) | sensitive | 2 | ameesh-cx2 (codex, #14987) + auditeur (claude, #14994) | mty-amh-impl-04 (deepseek) | L93 : my workstation |
| #2934 | `e5a37a8e7bd9` (gel 15056) | sensitive | 3 | ameesh-cx3 (codex, #15060) | mty-amh-docs-01 (deepseek) | note « Budget pause » : une note par pause, pas par variation de pourcentage |
| #2907 | `16f5ead34455` (gel 15011) | sensitive | 3 | auditeur (claude, #15017) + mty-amh-qa-01 (deepseek, #15025) | ameesh-cx2 (codex) | panier #2892 : défauts non bloquants (psycopg) |
| #2922 | `ed78cb936f22` (gel 14992) | light | 3 | ameesh-cx2 (codex, #14999) | mty-amh-impl-08 (deepseek) | format canon d'un workflow Process exécutable |
| #2894 | `30f74e812374` (gel 14945) | light | — | ameesh-cx2 (codex, #14948) | mty-amh-impl-08 (deepseek) | API de contexte et d'observation du tour : note de conception |
| #2933 | `ea09a09afe2a` (gel 15053) | sensitive | 3 | auditeur (claude, #15057) + mty-amh-qa-01 (deepseek, #15068) | ameesh-cx2 (codex) | alertes ménage : clé stable de tmp_orphan et worktree_kept |

Avis et classes lus par `scripts/paquet-candidats.py` (porte §2 : `ok` pour
chaque lot admis, avis requis sur le gel exact, aucun avis d'un auteur).

## Composition (9 unités)

| unité | lot | source | commit | note |
|---|---|---|---|---|
| 1 | #2847 | `b98b89b09f24` | `e2cb3bafb` | 0 conflit |
| 2 | #2931 | `3745404e02fd` | `490fe58f5` | 0 conflit |
| 3 | #2919 | `4ea0a6967449` | `5d3306b8a` | 0 conflit |
| 4 | #382 | `5d79c1960323` | `218ae9e7d` | 0 conflit |
| 5 | #2934 | `e5a37a8e7bd9` | `2ca890c2a` | 0 conflit |
| 6 | #2907 | `16f5ead34455` | `a8f8ad177` | 0 conflit |
| 7 | #2922 | `ed78cb936f22` | `9bd0c7ed3` | 0 conflit |
| 8 | #2894 | `30f74e812374` | `00b19a7e5` | 0 conflit |
| 9 | #2933 | `ea09a09afe2a` | `6557fef7c` | 0 conflit |

Chaque commit de composition a le lot en second parent, son sujet nomme le lot
et son SHA, et il porte un trailer `ameesh-work: <lot>`.

## Résolutions de composition

Aucune : 9 fusions propres.

## Adaptations de composition (2)

| source | changement | preuve |
|---|---|---|
| `src/ameesh/train_contracts.py` | blob épinglé de `.github/workflows/tests.yml` : `8403f902…` → `6a420c0d…` | #2907 (`16f5ead34455`) modifie `tests.yml`, relu par auditeur (claude) + mty-amh-qa-01 (deepseek) sur ce gel ; le changement ne touche que l'installation du venv (psycopg ≥ 3.2, cache 3.1 invalidé), pas les jobs ni les modules exécutés. Le contrat #2446 exige le blob audité : `tests.test_2446_train_contract` est vert après la mise à jour. Précédents identiques : `f88fcf338` (h-0000), `85fad987b` |
| `src/ameesh/main.py` | entrée `process check` de l'aide racine déplacée à son rang alphabétique (avant `progress`) | #2922 (`ed78cb936f22`) l'insère après `canon` ; `tests.test_2179_roadmap_cli` exige l'aide racine triée (rouge de la première CI, run 39532579770, part psycopg 4/8) ; texte inchangé, déplacement seul, **3 tests OK** |

## Écartés (ce véhicule)

| lot | source | motif |
|---|---|---|
| #2916 | `48fb429770f7` | conflit de composition avec #2919 (composé avant lui, même priorité P2) sur `src/ameesh/slo.py` et `src/ameesh/exploitation.py` ; une remise de #2916 sur `main` après ce paquet, relue sur son delta, rejoint le paquet suivant |

## En attente au calcul (non admis)

| lot | motif |
|---|---|
| #114 | `verdicts-missing` |
| #286 | `blocked-verdict` |
| #2614 | `blocked-verdict` |
| #2771 | `verdicts-missing` |
| #2892 | `verdicts-missing` |
| #2895 | `blocked-verdict` |
| #2900 | `verdicts-missing` |
| #2905 | `verdicts-missing` |
| #2906 | `verdicts-missing` |
| #2914 | `blocked-verdict` |
| #2924 | `blocked-verdict` |
| #2935 | `blocked-verdict` |

## Contrôles

- Liste calculée `scripts/paquet-candidats.py --repo . --target main` sur
  `e898caeb58fa` : **10 prêts, 12 en attente, 0 erreur** (#2933 devenu prêt
  entre deux calculs, composé avant la première poussée).
- Contenu : 56 fichiers, +5576/−193 hors manifeste ; aucune migration SQL.
  `git diff --check` ne signale que `src/ameesh/ui/static/ameesh-ui.js`
  (bundle généré par #382, espaces dans des gabarits littéraux).
- Ciblés sur l'arbre composé (local, sans CI), tous verts :
  13 modules (unités #2847, #2931, #2919, #382, #2934, #2907, #2922, #2894,
  témoins merge-pr, désignation, preuve de fusion) : **350 tests, OK** ;
  puis, sur la tête finale avec #2933 : `test_2933`, `test_789_slo*` et
  8 modules de ménage : **260 tests, OK** (1 ignoré).
- Contrat CI #2446 : `tests.yml` modifié par #2907 sans mise à jour du blob
  épinglé (rouge de la première CI, run 39532579770, part psycopg 6/8) ;
  adaptation mécanique du blob seul dans `train_contracts.py` (voir
  ci-dessus), **2 tests OK**, puis suite complète relancée sur la nouvelle tête.
- Aide racine : `process check` (#2922) hors de l'ordre alphabétique exigé
  par `test_2179_roadmap_cli` (même run, part psycopg 4/8) ; entrée déplacée
  sans changer son texte, `RoadmapHelpTest` et `test_l230_build_artifacts`
  verts.
