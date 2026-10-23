# Paquet integration/paquet-h-20261020-0500 — composition (§12)

Base : `main` **`11020744562f`** (fusion du paquet h-0300, PR #2139, 20/10
02:51Z). Branche `integration/paquet-h-20261020-0500`, cible `main`. Une seule
suite complète est demandée sur la tête finale.

Demandes : bureau 03:04Z — « h-0300 fusionné (04:51, 10 lots dont #2766). 6 lots
ameesh de nouveau "waiting: merge" : compose le paquet suivant (h-0500) ; je le
fusionne au vert. » Les six lots signalés par le bureau sont #2827, #2789,
#2801, #2814, #2816 (admis à la coupure) et #2786 (écarté, voir « Écartés »).

Liste calculée `scripts/paquet-candidats.py` à 03:09Z sur `11020744562f` :
**5 prêts, 7 en attente, 0 erreur** (`rc 0`, `--ready-out` écrit).

## Recomposition après la CI rouge du 20/10 03:25Z

La première suite complète (run `39363432266`, tête `299bbcd866`, 5 unités) a
relevé trois échecs pour deux causes, toutes deux portées par une unité :

| rouge | cause | unité |
|---|---|---|
| `tests.test_cluster.SuperviseurRoleTest.test_chaque_colonne_est_accordee_ou_exclue` (psql 11/16, psycopg 5/8) | `0469_host_cgroup.sql` ajoute `host_resources.cgroup` sans la classer au contrat des rôles superviseur | #2814 |
| `tests.test_l88_ui.ComponentContractTest.test_catalogues_have_the_same_keys` (psycopg 7/8, psql 10/16) | `chat.unread` pluralisé dans `fr.json` (`one`/`other`) mais `en.json` laissé en chaîne | #2801 |

Application de §12 et §11 bis (« lot fautif retiré sans relire le reste ») :
les deux unités sont **retirées de ce véhicule**, sans relecture du reste et
sans correctif de composition. Leurs têtes gelées ne sont pas modifiées ; le
diagnostic est remis à leurs auteurs pour le paquet suivant (voir « Retirés
après la CI rouge »).

Le véhicule est recomposé sur la même base avec **3 unités, 3 lots** (#2827,
#2789, #2816), fusions propres, aucune résolution, aucune adaptation : le seul
contenu hors fusions est ce manifeste. **Aucun delta propre de véhicule** : la
porte #2766 lit les avis des trois membres sur leur tête gelée et n'exige aucun
avis sur la tête du paquet ; `ameesh ci run --full --avis 0` est admissible par
le manifeste (porteur `integration/**`).

## Admissions (3 unités, 3 lots portés)

| lot | source | classe | avis | motif |
|---|---|---|---|---|
| #2827 | `96fa23faec3b` (tête gelée) | light | dev5 (DeepSeek) | P1 ménage : garder les clones qui portent des commits locaux hors refs distantes (tag/ref) ; auteur cx3 |
| #2789 | `6c18d126df0a` (tête gelée) | sensitive | dev5 (DeepSeek) + cx1 (Codex) | exécuteur : le brief d'un restart survit au redémarrage de l'exécuteur de l'hôte ; auteur mesh-design |
| #2816 | `eb2fe0c2ae16` (tête gelée) | light | auditeur (Claude) | panier #2757 : guide `plan_underused` et projection sous débit plafonné ; auteur impl-04 |

## Composition (3 unités)

| unité | lot | source | commit | note |
|---|---|---|---|---|
| 1 | #2827 | `96fa23faec3b` | `b52dc0daf5d3` | light ; 0 conflit |
| 2 | #2789 | `6c18d126df0a` | `54bc00bd518c` | sensitive ; 0 conflit |
| 3 | #2816 | `eb2fe0c2ae16` | `d5439f7e3571` | light ; 0 conflit |

Chaque commit de composition a le lot en second parent, son sujet nomme le lot
et son SHA, et il porte un trailer `ameesh-work: <lot>`
(`scripts/paquet-compose.py --units --base origin/main` : 3 unités de fusion,
3 lots portés).

## Résolutions de composition

Aucune : les trois fusions sont propres (`git status` sans conflit, aucun
marqueur). Le seul contenu hors fusions est ce manifeste.

## Retirés après la CI rouge (diagnostic remis au paquet suivant)

| lot | tête gelée | rouge | correctif attendu |
|---|---|---|---|
| #2801 | `42e2c4a585c4` (remise `main-11020744`) ; gel `7b0b8a24` | `test_l88_ui.ComponentContractTest.test_catalogues_have_the_same_keys` (psycopg 7/8, psql 10/16) | aligner `ui/src/i18n/en.json` sur le pluriel de `fr.json` (`one`/`other`, `{count} unread`), reconstruire `src/ameesh/ui/static/ameesh-ui.js`, puis re-geler et faire relire le delta (léger, 1 avis) |
| #2814 | `8dc6c370ccb1` | `test_cluster.SuperviseurRoleTest.test_chaque_colonne_est_accordee_ou_exclue` (psql 11/16, psycopg 5/8) | classer `host_resources.cgroup` : l'ajouter au contrat `deploy/sql/role-superviseur.sql` (mesure JSON, pas un secret) ou, si elle doit rester fermée, dans `EXCLUES` (`tests/test_cluster.py`) ; puis re-geler et refaire relire (sensible) |

Ces deux lots restent en `qa`, en attente de fusion ; ils ne sont pas fusionnés
par ce véhicule.

## Écartés (ce véhicule)

| lot | motif |
|---|---|
| #2786 | conflit avec `main` sur `src/ameesh/plan_git.py` ; remise `main-797779c2` (`018799cd`) refusée : classe sensitive, un seul avis (auditeur, Claude) sur le delta de la remise, non mécanique — un second avis d'une autre famille est requis ; au paquet suivant après revue du delta |
| #1515 | conflit avec `main` (`src/ameesh/fire.py`, `src/ameesh/merge_queue.py`, `tests/test_1003_merge_pr.py`), aucune remise sur `110207445` ; verdicts acquis mais non composable en l'état |
| #114 | verdicts-missing : un seul avis (impl-07, DeepSeek) sur le gel `89bdc6b50e93`, deux familles requises (sensible) |
| #2614 | no-verdict : gel effectif refusé |
| #2665 | verdicts-missing : aucun avis sur le gel `6d88778bdb25` (sensible, un avis d'une autre famille requis) |
| #2771 | no-verdict : gel effectif refusé |
| #2792 | verdicts-missing : un seul avis (auditeur, Claude) sur le gel `e886568eccf1`, deux familles requises (sensible) |

## Contrôles

- Liste calculée `scripts/paquet-candidats.py`, base `11020744562f` :
  **5 prêts, 7 en attente, 0 erreur** (`rc 0`) ; 2 des 5 prêts sont retirés
  après la CI rouge, 3 composés.
- Composition : **3 unités, 3 lots** ; aucun conflit, aucune résolution.
- `git diff --check` : propre ; aucun bundle minifié dans ce véhicule.
- `scripts/garde-mecanique.py --json` : **0 constat**.
- `scripts/doc-links.py` : **0 lien cassé**.
- `scripts/freeze-checks.py` : **6 contrôles verts** (garde mécanique,
  compilation, attribution #1632, lint L63, types d'alerte #1060, lecteurs de
  la doc L161a).
- UI sur l'arbre composé : `npm test` **63/63**, `npm run check` vert.
- Migrations : **aucune** (la `0469_host_cgroup.sql` repart avec #2814).
- Tests ciblés Python : `tests.test_cluster` **34 tests, 0 échec** (6 sautés),
  modules #2827, #2789, #2816 et `tests.test_l88_ui` **26 tests, 0 échec** —
  **60 tests, 0 échec** (6 sautés) sur le pilote psql local.
- Prévalidation : indisponible (card-stale, carte > 48 h) ; sélection non sûre,
  la suite complète est demandée (`--full`), motif journalisé.
- Contrôle d'inclusion `scripts/paquet-inclusion.py` (manifeste, tête de
  composition, attendus et prêts) : **aucun défaut** — 3 compositions,
  2 exclusions consignées et lues (#2801, #2814), contenus à la tête :
  **3 présents, 0 partiel**.
