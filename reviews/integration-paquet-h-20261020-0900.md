# Paquet integration/paquet-h-20261020-0900 — composition (§12)

Base : `main` **`cfa8b2f439fd0eab3af6f41ee9dba5b36a68bdef`** (fusion du paquet
h-0800, PR #2169, 20/10 07:37:22Z). Branche
`integration/paquet-h-20261020-0900`, cible `main`. Une seule suite complète
est demandée sur la tête finale.

Demandes : bureau 07:55:54Z (message #118983) — « h-0800 fusionné (09:37).
6 lots ameesh « waiting: merge » : compose h-0900 maintenant pour qu'il entre
si possible avant la coupure de 11:00 ; je le fusionne au vert. »
Orchestrateur 07:56:43Z (message #118987, urgent) — « #2861 (P1, correctif de
#2844 déjà sur main) doit y entrer dès l'avis cx1, AVANT la release staging
de 11:00 » (sinon les commentaires d'étape de #2844 rejouent tout l'historique
sur chaque ticket miroir ouvert au déploiement).

## Liste calculée

- 07:58Z sur `cfa8b2f43` : **3 prêts** (#2843, #2851, #2808), 8 en attente,
  **1 lecture impossible** (#1515 : provenance d'auteur inconnue pour
  `ab4f52ee1c05`, commit de la branche `d4b1a43da94b`) ; `rc 1`,
  `--ready-out` retiré (#2404).
- 08:03Z sur la tête composée `ad83db765` (après #2808) : **0 erreur** ;
  #1515 et #2786 deviennent prêts — la provenance `ab4f52ee1c05` sort de la
  plage de gel, et le conflit `src/ameesh/plan_git.py` de #2786 ne subsiste
  plus (`merge-tree` propre). #2861 reçoit l'avis cx1 à 08:00:17 sur
  `a83bc87d32e2`.
- 08:08Z sur la tête finale `c7bf7235b4c6` : **0 prêt hors paquet, 0 erreur**,
  `--ready-out` vide (calcul valide) ; les six lots composés sont
  `already-in-target`.

## Admissions (6 unités, 6 lots portés)

| lot | source | classe | avis | motif |
|---|---|---|---|---|
| #2843 | `3e9ca9fd6012` (tête gelée) | sensible (1 avis) | cx3 (Codex) OK 07:45 | panier #2839 : âge par fenêtre et courte vivante dans l'échantillon des comptes codex |
| #2851 | `40d23939a07c` (tête gelée) | sensible (2 avis) | auditeur (Claude) OK 07:08 + cx3 (Codex) OK 07:10 | `ameesh alerts --json` sous 60 s sur pc-smichea (lecture rapide) |
| #2808 | `d4b1a43da94b` (tête gelée) | sensible (2 avis) | auditeur (Claude) OK 06:48 + cx2 (Codex) OK 07:12 | auteur de référence au gel : seule une preuve de commit compte, pas le titulaire |
| #1515 | `21881c219ab1` (tête gelée) | sensible (2 avis) | mesh-design (Claude) OK 07:16 + cx1 (Codex) OK 07:18 | porte des avis : auteur = assigné, périmètre critique, doute, commentaire doublé ; dépend de #2808, composé dans le même véhicule |
| #2786 | `018799cd7522` (remise `refs/ameesh/resolutions/2786/main-797779c2`) | sensible (2 avis) | auditeur (Claude) OK 02:40 + cx1 (Codex) OK 07:59 | gel « HEAD » littéral : contrôle `full_commit_id` et composition de paquet ; remise relue (remerge-diff), conflit `plan_git.py` résolu |
| #2861 | `a83bc87d32e2` (fb8c336f + correction de relecteur) | léger (1 avis) | cx1 (Codex) OK 08:00 | #2844 : un seul « Frozen for review. » par lot ; inscription d'un ticket préexistant = seul l'état courant, le reste marqué vu |

## Composition (6 unités)

| unité | lot | source | commit | note |
|---|---|---|---|---|
| 1 | #2843 | `3e9ca9fd6012` | `021f60a79c8f` | sensible ; 0 conflit ; 4 fichiers, +259/−24 |
| 2 | #2851 | `40d23939a07c` | `a4a2f0fb5328` | sensible ; 0 conflit ; 15 fichiers, +504/−36 |
| 3 | #2808 | `d4b1a43da94b` | `ad83db76526c` | sensible ; 0 conflit ; 6 fichiers, +408/−30 |
| 4 | #1515 | `21881c219ab1` | `bd0641d77a3e` | sensible ; 0 conflit ; 11 fichiers, +1000/−52 |
| 5 | #2786 | `018799cd7522` | `84e1a8324524` | sensible ; 0 conflit ; 20 fichiers, +1011/−34 |
| 6 | #2861 | `a83bc87d32e2` | `c7bf7235b4c6` | léger ; 0 conflit ; 2 fichiers, +41/−5 |

Chaque commit de composition a le lot en second parent, son sujet nomme le lot
et son SHA, et il porte un trailer `ameesh-work: <lot>`
(`scripts/paquet-compose.py --units --base cfa8b2f43` : 6 unités de fusion,
6 lots portés : #2843 3e9ca9fd6012, #2851 40d23939a07c, #2808 d4b1a43da94b,
#1515 21881c219ab1, #2786 018799cd7522, #2861 a83bc87d32e2).

## Résolutions de composition

Aucune : les six fusions sont propres (`git status` sans conflit, aucun
marqueur de conflit). Le seul contenu hors fusions est ce manifeste.

## Écartés (ce véhicule)

| lot | motif |
|---|---|
| #114 | `verdicts-missing` : un seul avis sur le gel `89bdc6b50e93`, deux avis de familles distinctes requis (sensible) |
| #2771 | `no-verdict` : gel effectif refusé (`no-verdict`) |
| #2614 | `no-verdict` : gel effectif refusé (`no-verdict`) |
| #2814 | `verdicts-missing` : un seul avis (auditeur, Claude) sur `31739fe46624`, deux avis requis (`sensitive`) ; cx2 attendu |
| #2845 | `blocked-verdict` sur `d0f3f0eb67b0` et conflit `tests/test_l126_issues.py` ; reprise après nouvelle tête gelée |
| #2853 | `missing-branch` : incident Nexlink (PR #1849 fusionnée avant le vert) sans branche dans ce dépôt ; suivi au lot Nexlink |
| #2857 | `branch-not-published` : canon §12 du composeur délégué, branche `mesh-design/composeur-delegue` portée par `manaty/home#138`, pas par `ameesh` ; rien à composer ici |

## Contrôles

- Liste calculée `scripts/paquet-candidats.py` : voir « Liste calculée »
  ci-dessus ; le calcul final sur la tête rend **0 prêt, 0 erreur** (`rc 0`,
  `--ready-out` vide, fichier présent).
- Composition : **6 unités, 6 lots** ; 51 fichiers, +3209/−167 hors manifeste ;
  aucun conflit, aucune résolution ; `git diff --check` ne relève rien.
- `scripts/paquet-inclusion.py` (manifeste, tête, attendus et prêts) :
  **aucune omission** (`omissions`, `ready_omissions`, `removed` vides ;
  `errors` vide) — 6 compositions ; contenus à la tête : 5 présents,
  **1 partiel attendu** (#2808 : 3 lignes de `merge_queue.py` et 3 de
  `test_2808` adaptées par #1515, qui contient déjà #2808 ; `--strict` non
  demandé) ; 0 exclusion.
- `scripts/garde-mecanique.py --json` : **0 constat**, 0 exemption inutilisée.
- `scripts/doc-links.py` : 172 fichiers, **0 lien cassé**.
- `scripts/freeze-checks.py` : **6 contrôles verts**.
- Tests ciblés Python sur l'arbre composé (pilote psql local, les 26 modules
  touchés ou de contrat) : **à compléter par le résultat du run** ; la suite
  complète les couvre en CI.
- Migrations : **aucune** au diff (`git diff --name-only` sans `.sql` ni
  `migrations/`).
- UI : aucun fichier UI au diff.
- Prévalidation : indisponible (carte périmée 63 h) ; sélection non sûre, la
  suite complète est demandée, motif journalisé.
- Suite complète unique demandée sur la tête finale (`ci-full-<sha>`), avec la
  revue unitaire du paquet pour seul avis de la demande de CI (§12,
  `--full --avis 0`).

## Notes d'activation

- #2861 (P1) : le correctif doit être dans la release staging de 11:00 — au
  déploiement, l'inscription d'un ticket miroir déjà ouvert ne doit plus
  rejouer `accepted`, chaque re-gel et chaque attente ; un seul « Frozen for
  review. » par lot, le reste marqué vu. Aucun geste de données.
- #2786 : la source composée est une remise relue ; aucun geste de données.
- #1515, #2808 : modifications de la porte des avis et de l'attribution ;
  aucun geste de données ni migration.
- #2843, #2851 : comptes/coûts et `alerts` ; aucun geste de données.
