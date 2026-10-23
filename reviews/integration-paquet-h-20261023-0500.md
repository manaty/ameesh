# Paquet integration/paquet-h-20261023-0500 — composition (§12)

Base : `main` **`45dbed1e361b`** (fusion du paquet h-20261023-0200, PR
manaty/ameesh#2321, tête `6fa9388fe823`). Branche
`integration/paquet-h-20261023-0500`, cible `main`, lot porteur **#3052**.
Une seule suite complète sur la tête finale.

Composeur : mty-amh-impl-01, sur le courrier mesh-design du 23/10 03:02Z
(« Tu restes le composeur. Compose h-0500 maintenant ») et la liste calculée
`scripts/paquet-candidats.py` du 23/10 03:05Z sur la base `45dbed1e3` ;
canon §11 bis et §12.

## Unités (8 unités, 8 lots portés)

| lot | source | classe | avis (familles) | auteur | objet |
|---|---|---|---|---|---|
| #2989 | `bae16e9f273e` (gel 15578) | sensitive | 2/2 ok (codex cx2, claude dev2) | mty-amh-impl-05 | Porte #2924 : ignorer un gel superseded absent du dépôt (gel fantôme `66a06cf0`) |
| #3001 | `1df6e8fd0279` (gel 15710) | sensitive | 2/2 ok (codex cx2, deepseek dev3) | ameesh-dev2 | Clone radar : origin = URL de la forge, alternates gardés ; migration des clones existants |
| #3034 | `11d616ab7b1d` (gel 15728) | sensitive | 1/1 ok (claude dev1, porte train) | mty-amh-impl-05 | Jauges Codex : forfaits team/pro sous une identité unique, plus d'« ambigu » |
| #3039 | `ecb2a787517d` (gel 15719) | sensitive | 2/2 ok (deepseek dev3, claude dev1) | ameesh-cx3 | Staging : le lancement depuis ameesh-release.service passe la garde #2950 au retour du poste |
| #3004 | `aba5f7de4092` (gel 15528) | sensitive | 2/2 ok (codex cx2, claude dev2) | mty-amh-impl-02 | Désignation au gel : la charge compte aussi les avis en attente |
| #3016 | `8e710c30cd81` (gel 15564) | light | 1/1 ok (claude dev1) | ameesh-dev3 | EP8 : étude — réplica de la base du mesh et sa bascule |
| #3027 | `914d7678e824` (gel 15784) | sensitive | 2/2 ok (claude dev1, codex cx2) | ameesh-dev5 | Panier « #393 » du 2026-10-23 : boucle macOS et contrat #2446 |
| #2920 | `195a1f394953` (gel 15534) | sensitive | 2/2 ok (codex cx2, claude dev4) | mty-amh-impl-05 | Panier « #2893 » du 2026-10-21 : spend legacy |

## Admissions

#2989, #3001, #3034, #3039, #3004, #3016, #3027, #2920

## Composition (8 unités)

| unité | lot | source | commit | note |
|---|---|---|---|---|
| 1 | #2989 | `bae16e9f273e` | `4c48eee0429f` | fusion de la tête gelée, 0 conflit |
| 2 | #3001 | `1df6e8fd0279` | `2832cb24085b` | fusion de la tête gelée, 0 conflit |
| 3 | #3034 | `11d616ab7b1d` | `a0f61108eebf` | fusion de la tête gelée, 0 conflit |
| 4 | #3039 | `ecb2a787517d` | `e9014dc44167` | fusion de la tête gelée, 0 conflit |
| 5 | #3004 | `aba5f7de4092` | `6755ac48b90a` | fusion de la tête gelée, 0 conflit |
| 6 | #3016 | `8e710c30cd81` | `7ee36d19917f` | fusion de la tête gelée, 0 conflit |
| 7 | #3027 | `914d7678e824` | `aaf9300f65e7` | fusion de la tête gelée, 0 conflit |
| 8 | #2920 | `195a1f394953` | `7d6648f69dd6` | fusion de la tête gelée, 0 conflit |

Chaque commit de composition a le lot en second parent, son sujet nomme le lot
et son SHA, et il porte un trailer `ameesh-work: <lot>`. Arbre composé :
28 fichiers, +2136/−70 depuis `45dbed1e3`.

## Écartés (ce véhicule)

| lot | source | motif |
|---|---|---|
| #2994 | — | **erreur de lecture de la porte** : provenance inconnue des gels `5158130ae2da`, `d8c377cf31a4` ; attend la fusion de #2989 puis un regel (courrier mesh-design 05:02Z). |
| #2914 | — | **erreur de lecture de la porte** : provenance inconnue du gel fantôme `66a06cf09d5c` ; attend la fusion de #2989 puis un regel (courrier mesh-design 05:02Z). |
| #3007 | `0a8a5747d03a` | non prêt au calcul 05:05Z : **conflit** avec main sur `docs/EXECUTEUR-MEDIEE.md` ; hors véhicule, à recomposer. |
| #3010 | `6a8f19097339` | non prêt au calcul 05:05Z : **conflit** avec main sur `docs/EXECUTEUR-MEDIEE.md` ; hors véhicule, à recomposer. |

## En attente au calcul (non admis)

| lot | motif |
|---|---|
| #114 | verdicts-missing (2) + conflit `src/ameesh/exploitation.py` |
| #390 | verdicts-missing (2) + conflit `.github/workflows/tests.yml`, `site/docs/guides/install.md` |
| #2614 | verdicts-missing (un seul avis codex) |
| #2771 | verdicts-missing (un seul avis codex) |
| #2940 | verdicts-missing (aucun avis) |
| #2954 | verdicts-missing (aucun avis) |
| #2955 | blocked-verdict |
| #2987 | branch-not-published (`canon/2987-porte-prod-main`) |
| #2993 | verdicts-missing (aucun avis) |
| #3008 | verdicts-missing (un seul avis codex) |
| #3023 | verdicts-missing (aucun avis) |
| #3035 | blocked-verdict |
| #3036 | verdicts-missing (aucun avis) |
| #3038 | verdicts-missing (aucun avis) |

## Contrôles

- Calcul du 23/10 03:05Z sur `45dbed1e3` : **8 lots prêts**, 16 en attente,
  **2 erreurs de lecture** (#2994, #2914). L'outil retire son export
  `--ready-out` dès qu'une lecture échoue (#2404) ; la liste des 8 prêts est
  reprise du calcul complet (tous les autres lots ont été évalués sans erreur),
  et les deux erreurs sont écartées avec motif ci-dessus. Aucune lecture
  globale n'a échoué.
- Liste prête utilisée pour le contrôle d'inclusion :
  `2989,3001,3034,3039,3004,3016,3027,2920`.
- Contrôle d'inclusion (`scripts/paquet-inclusion.py --manifest … --head 7d6648f69dd6 --ready-file prets-0500.txt --no-expected`, tête de composition avant ce manifeste) : **aucun défaut** — 8 entrées de composition, verdicts « présent » 8/8, 8 lots prêts, 8/8 contenus à la tête ; exclusions #2994, #2914, #3007, #3010 consignées.
- Contrôles statiques du gel (`scripts/freeze-checks.py`) : rejoués par le gel
  de la tête finale ; le gate les exige.
- Tests ciblés : après la CI rouge, les bancs des quatre incompatibilités ont
  été rejoués localement (166 tests, tous verts) ; la suite complète de la CI
  sur la tête finale fait foi.

## CI rouge sur 427d165ba (23/10 03:15Z–03:30Z)

La première suite complète a échoué sur quatre incompatibilités entre unités,
toutes dans des bancs de test — aucun code produit modifié.

| unités | symptôme | correctif |
|---|---|---|
| #3034 | `TypeError: _Book.codex_gauges() got an unexpected keyword argument 'plan'` (8× `test_1176`, 5× `test_1227`, 2× `test_1075`, 2× `test_2648`) | les quatre doubles de test acceptent `plan=None` |
| #2989 × #3004 | `freeze refused (head-unresolved)` sur les SHA synthétiques de `PendingLoadTest` | le témoin gèle de vrais commits de son dépôt (`repo=`) et fixe `branch_target` |
| #3039 | le banc #1323 laisse le vrai `systemd-run` échouer (pas de bus utilisateur), la séquence finit en retour | lanceur direct `systemd-run` dans le `PATH` du banc, comme #1155 |
| #3004 × #2541 | le rejeu forcé choisit rev3 (charge 0) au lieu de rev1 (charge 1) | rev3 reçoit un lot ouvert ; rev1 reste le moins chargé à nom égal |

Ciblés rejoués le 23/10 : 1176 35/35, 1227 12/12, 1075 17/17, 2648 3/3,
`PendingLoadTest` 4/4, `StagingSignalTest` 9/9, `ForceReplayMailTest` 4/4,
1155+2950 82/82. Aucun avis supplémentaire n'est requis : ces changements ne
touchent que des bancs de test (adaptation de test, §11 bis).

Correctif : `ab2caa7770c5` (7 fichiers de test, +74/−20). La tête de la
branche devient le commit qui porte ce manifeste ; la CI complète unique est
relancée sur elle.
