# Paquet integration/paquet-h-20261017-0300 — composition

Base : `main` après atterrissage de PR1564 (`transport/incident-20261017-0220`,
tête `90de1a20c49c8f6fc69a4406b498f7d433feea65`, merge réel à relever à la
notification). La branche est construite sur cette tête exacte, qui devient un
ancêtre de `main` par la fusion de PR1564 : le diff de la PR ne porte que les
unités ci-dessous.

Exécuteur : **mty-amh-impl-01** (composition, CI et fusion). Cutoff mesh-design
17/10 02:48Z : réception de la notification de fusion réelle de PR1564, aucune
nouvelle attente de liste ni de feu nominatif ; les admissions valent pour
leurs têtes/portées couvertes (§11bis/12). Ce manifeste est le périmètre de
référence ; une seule CI complète sur la tête gelée, puis fusion au vert.

## Composition

| unité | lot | source | commit de composition | note |
|---|---|---|---|---|
| 1 | #1900 | `95b9be232e11` (`lot/1900-suite-811`) | `29fd6d856` | N1/N2 ; GO cx1 sur la tête ; conflits avec main/h-0115 sur `docs/EXECUTEUR-MEDIEE.md`, `backlog.py`, `test_811_lot_workdirs.py` — union mécanique, compteurs 65/368/433 avant #1425 |
| 2 | #1982 | `d5ac0c61157e` (`lot/1982-slo-faux-positifs`) | `c6533f01c` | points 1+2, GO dev2 ; fusion propre |
| 3 | #379 | `05700a0eb5b0` (`lot/379-decisions-approvals`) | `b01dfc8ee` | gel/GO cx2 ; remises 1343+1466 conservées ; fusion propre |
| 4 | #2086 | `8dbc6c80278e` (`agent/deepseek4-2086-stages`) | `08fcb90fe` | GO Codex ; fusion propre |
| 5 | #1720 | `b50477e5da30` (`refs/ameesh/resolutions/1720/port-main-700ca2f47`) | `e5e3e53a5` | GO Claude dev4 ; port, fusion propre |
| 6 | #1600 | `f62472cbdceb` (`remise/1600-main-edf2bf3d`) | `1c22582f4` | remise union `1280d07c`, GO Codex ; fusion propre |
| 7 | #1859 | `2c9943db5d8d` (`lot/1859-auteur-gel-detenteurs`) | `c54c9b22a` | GO dev4 (courrier 92824) ; fusion propre |
| 8 | #2104 | `68df8f013248` (`lot/2104-radar-conflict-attribution`) | `6e5a9fbf2` | GO autre famille (courrier 92822) ; fusion propre, `resolutions.py` compatible avec #2165 |
| 9 | #1425 | `a2804c84736a` (`lot/1425-suites-753`) | `1e79656c1` | conflits avec #1900/main : `runner.py` (ordre : dessaisissement #1425 avant prise #804), `storage/interface.py` (union), `operations.py` (`_LOTS_ACTIVITY` commun + `workflow_ref`/#1922 conservés), `contract.json` (pas d'`interface_total`, dérivé), doc 65 ops / 370 refusées / 435 interface |
| 10 | #2165 | `f28c268f4a57` (`agent/deepseek2-2165-rr-preimage`) | `b24690f90` | GO cx1, cache v3 ; fusion propre, paire #1349 `a105ab60` disponible |
| 11 | #2164 | `d235a7711e5c` (`agent/deepseek4-2164-approval-expiry`) | `57b117369` | après #379, GO croisés ; fusion propre |
| 12 | #2174 | `2ba6d2f8be54` (`lot/2174-disque-vm-agents`) | `2fb167eca` | PR1566, lectures croisées ; conflit avec #2167/PR1564 sur `housekeeping.py` — union : garde tôt `worktree_in_use` (#2167), puis `foreign and not pushed` (#2174) et re-contrôle `worktree_in_use` |

## Résolutions de composition (preuve)

- #1900 : les trois conflits sont résolus par la résolution déjà enregistrée
  (rerere) ; le contrat reste à 65 opérations admises, 368 refusées, interface
  433 avant #1425.
- #1425 : union mécanique sur 5 fichiers ; `test_l107_executor_contract` local
  (sans base) **22/22 OK** sur l'arbre composé, ce qui valide les compteurs
  65/370/435 et la classification de `state_lots_activity`.
- #2174 : les deux gardes de `housekeeping.py` sont conservées (contrôle tôt
  des processus, puis contrôle `foreign` non poussé et re-contrôle) ; aucun
  autre conflit.
- #2104 et #2165 touchent tous deux `resolutions.py` : fusion automatique
  propre, pas de résolution manuelle.

## Hors périmètre (non retenus à ce cutoff)

- #1945/#2171 : conditionnels au GO dev7 exact sur #1945 ; non acquis.
- #366 : conditionnel au complément indépendant DeepSeek (impl06) ; non acquis.
- #753 : conditionnel à #1467/#1465 finaux, dont la correction du contrat des
  colonnes de rôles de #1465 ; non poussée à ce cutoff.
- #2179, #2152 : explicitement hors liste prête par le pilote.
- Aucun lot non admis n'est inclus ; pas d'extension de PR1564.

## CI et fusion

- Une seule CI complète sur la tête gelée de la branche (PR `pull_request`,
  suite complète psql+psycopg) ; aucune CI de lot.
- Fusion au vert sur la tête exacte (auto-fusion) ; rattachement des lots aux
  commits réels à l'atterrissage.
