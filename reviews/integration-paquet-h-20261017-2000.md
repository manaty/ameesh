# Paquet integration/paquet-h-20261017-2000 — composition

Base : `main` **`f8c8a4a08`** (fusion de la PR #1679, paquet h-1900 ; parents `210ec24ac` ×
`bea7b7fc`). La branche est composée directement sur cette base ; le diff de la PR ne porte que
les 6 unités ci-dessous et ce manifeste.
Tête de composition (6 unités) : **`c2a7e99b5`** ; la tête de branche au gel, qui porte ce
manifeste, est le SHA exact communiqué au pilote et couvert par l'unique suite complète du paquet.

Demandes : **pilote mesh-design** — admissions du 17/10 : #2275 et #1907 « premier paquet horaire
après main verte » (notes 17:39/17:47 et 17:36), #380 avec verdict reposé sur `566b6193` (mail
21:14Z), #2154 juste après #380 (auditeur, mail #101420 20:58Z ; avis mesh-design sur
`2086a9c6`), #1630 ajouté à 21:10Z (verdict mesh-design, CI de lot verte), #2218 admis le 17/10
16:41 (note du lot 14:41) et confirmé mail 21:15Z. La composition attendait l'atterrissage de
h-1900 (fait à 21:39:10Z, `M2 = f8c8a4a08`) et le vert du run de lot #380.

Exécuteur : **mty-amh-impl-01** (composition, ouverture de la PR, unique CI de paquet, remise au
pilote). Aucune CI de lot, aucun push de branche de lot, aucune fusion parallèle.

## Admissions (6 lots)

- #2275
- #1907
- #380
- #2154
- #1630
- #2218

## Composition (6 unités)

| unité | lot | source | commit | note |
|---|---|---|---|---|
| 1 | #2275 | `11cd24d8331cbd0d845840b0f0b2bde508ea06d8` (`lot/2275-garde-cgroup-020rc2`, PR #1653) | `d0a4db988` | avis `ok` ameesh-cx3 (Codex) + auditeur (Claude), auteur DeepSeek mty-amh-qa-01 ; classe `sensitive` (sécurité cgroup, `src/ameesh/deepseek_cgroup.cjs`) ; composition propre |
| 2 | #1907 | `92e8713f693587e617b7378e68c1d23e39c479f3` (`lot/1907-failed-before-stop`, PR #1627) | `9d5508662` | avis `ok` ameesh-dev2 (Claude) + mty-amh-qa-01 (DeepSeek), auteur ameesh-cx1 (Codex) ; classe `sensitive` prod (`deploy/mise-a-jour/**`) ; composition propre |
| 3 | #380 | `566b6193e18873449b7ecdadbccea67464e78f4e` (`lot/380-l92-paquet-guides`, PR #1688) | `2fa892216` | verdict `ok` mesh-design (Codex) reposé sur `566b6193` (mail 21:14Z) ; CI de lot `39069936576` verte ; classe `light` (25 f. ui/docs) ; composition propre |
| 4 | #2154 | `2086a9c662cf2c6d3f24a45b505176d3718aa4a2` (`review/mty-nxl-site-01-2154-ui-l92-finitions`, sans PR) | `ad53ee989` | verdict `ok` mesh-design (Codex) sur `2086a9c6` (gel 9894 / verdict 9896) ; classe `light` (7 f. ui/docs, recoupent #380) ; composition propre |
| 5 | #1630 | `cc5da85aa4cef2a77504f8e616acb5fa59c547e4` (`lot/1630-merge-pr-blocked-witnesses`, PR #1593) | `c97cfd17e` | verdict `ok` mesh-design sur `cc5da85a` ; CI de lot verte (tous checks `success`) ; classe `light` (1 f. de test, +6/−2) ; dépendance #1374 fusionnée ; composition propre |
| 6 | #2218 | `bc130ae3ca5d2e902b0aa3c3500b1f0bd3440a1f` (`lot/2218-critical-scope-path`, PR #1621) | `c2a7e99b5` | **deux avis** `ok` sur `bc130ae3` : ameesh-dev4 (Claude) + mty-amh-impl-04 (DeepSeek), auteur ameesh-cx2 (Codex) ; classe `sensitive` concurrence (porte de fusion, `merge_queue.py`) ; composition via la résolution `457567cac960e3d6a22516f1602749adc2bbc301` (`refs/ameesh/resolutions/2264/2218`, qa-01) ; 2 f. +53/−11 ; composition propre |

Chaque commit de composition a le lot en second parent, son sujet nomme le lot et il porte le
trailer `ameesh-work: #<lot>`. Aucun contenu nouveau hors la résolution #2264×#2218 (déjà validée
et versée dans la pré-composition). Total vs M2 : **36 fichiers, +2327/−109**, aucun `.github/**`,
aucune migration.

## Conflits et résolution

Aucun conflit : les 6 merges `--no-ff` passent rc=0 sur `f8c8a4a08`. La composition rejoue à
l'identique la pré-composition validée sur la base simulée `bc0f2cf11` (même arbre de base
`d0261fd`) : tête `f6f25aa5a`, arbre `a20d85fa`. L'arbre final de cette branche est **identique**
(`a20d85fa`), donc les preuves de la pré-composition valent pour ce contenu.

## Écartés (ce véhicule)

| lot | motif |
|---|---|
| #711 | remise `96c5b478` sur `210ec24a` prête (impl-05, 21:35Z) mais **admission du pilote non donnée** |
| #2207 | admission pilote non confirmée ; conflit connu avec #2281 (dans h-1900) |
| #1768 | exclu par le pilote (conflit `fire.py`/`radar.py`) ; rebase demandé, paquet suivant |
| #2122 | hors paquet tant que la source §11 bis n'est pas jugée |
| #2292 | correctif CI psycopg optionnel, non admis (h-1900 est passé) |
| autres lots `qa` | hors des admissions du pilote ; candidats aux paquets suivants |

## Preuves

- `git diff --check` : deux espaces finaux dans le bundle minifié généré
  `src/ameesh/ui/static/ameesh-ui.js` (lignes 2-3), **présents sur la branche #380** ; cosmétique,
  aucun job CI n'exécute `diff --check` (vérifié dans `.github/**` et `scripts/`). Aucun marqueur
  de conflit.
- Chaque unité est un merge `--no-ff` de la tête exacte listée ; seconds parents conformes (6/6,
  dont la résolution `457567ca` pour #2218) et trailers `ameesh-work` présents (6/6).
- Les têtes distantes des lots sont inchangées (`ls-remote` = sources listées).
- Preuves ui sur l'arbre `a20d85fa` (pré-composition, après symlink `node_modules`) :
  `npm test` **53/53**, `npm run typecheck` rc 0, `npm run check` rc 0 (63 275 o / 20 821
  compressés ; total 21 888 / 81 920). Contenu identique ici par construction.
- Tests ciblés Python : couverts par les CI de lot (#1630) et par la suite complète du paquet ;
  aucune reprise locale nécessaire, le contenu est celui de la pré-composition.
- Contrôle d'inclusion `scripts/paquet-inclusion.py --manifest <ce fichier> --head <tête>
  --expected 2275,1907,380,2154,1630,2218` : **aucun défaut détecté** (6 entrées). Le seul
  avertissement, « #380 contenu partiel à la tête », porte sur les lignes que #2154 (unité 4,
  empilée dans cet ordre sur demande de l'auditeur) réécrit dans les mêmes docs ui
  (`docs/ui/GUIDE-ELECTRON.md`, `docs/ui/GUIDE-PAGE-WEB.md`, `ui/README.md`,
  `ui/examples/acme-mesh/electron/renderer.mjs`) : c'est l'union validée en pré-composition, pas
  une perte. À rejouer sur la tête finale avant le feu.

## CI et fusion

- **Une seule** suite complète sur la tête exacte du paquet : poussée de
  `integration/paquet-h-20261017-2000` et PR ouverte immédiatement pour que le portier `paquet.yml`
  constate la couverture ; s'il ne la constate pas, sa suite `workflow_call` est l'unique suite et
  aucune étiquette n'est posée ; s'il la constate, l'étiquette `ci-full-<sha>` déclenche l'unique
  suite `tests.yml`. Dans les deux cas : une seule suite, sur ce SHA.
- Le check requis `tests` doit être `success` avec des tests réellement exécutés (pas
  `skipped`/absent, #2271). Aucune CI de lot, aucun second lancement.
- Feu du pilote sur ce SHA après `tests` vert (`gh pr merge --auto --match-head-commit <sha>` ou
  repli `ameesh work merge-pr`). Ne pas rebaser ; toute nouvelle tête annule le feu.
- Après fusion : rattachement des unités au merge réel et aux runs, clôture des lots, preuves aux
  auteurs et au pilote. Aucune opération hôte ici.
