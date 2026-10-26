# Paquet integration/paquet-h-20261026-31 — composition (§12), sur main issu de h-30 (v5 : 4 unités)

Base : `main` **`f444955b2eeb`** (fusion du paquet h-30, PR #2892, tête
`424b842aaa69`). Branche `integration/paquet-h-20261026-31`, cible `main`.
Porteur : **#3796**. Composeur : mty-amh-impl-01 (v1 à v3) ; recomposition
v4 et v5 par le pilote (mesh-design, claude, agents-2).

Méthode de h-30 : liste calculée `scripts/paquet-candidats.py` depuis la tête
exacte de `main`, tests locaux avant le gel, manifeste, contrôle d'inclusion,
`freeze-checks`, puis une seule `ci run --full --avis 0` sur la tête gelée.

## Historique des versions

- v3 `b57c0380` (7 unités) : le pilote annule la CI à 20:2xZ, parce que la
  porte a retenu l'auto-fusion sur #3506 (provenance de gel illisible, cas #3781).
- v4 `fc59a14e` (6 unités, sans #3506) : la ci-full 40397294387 a deux tests
  négatifs, reproduits en local. #3779 fait échouer
  `test_l224_session_harnais.CodexTest.test_compte_codex_secondaire` (écart de
  débuts de 30 s ; échoue sur main + #3779 seul). #3755 fait échouer
  `test_l107_executor_contract.TableTest.test_l_interface_entiere_est_classee`
  (`accounts.force_switch` non classé). Le pilote annule la CI à 21:1xZ, pose
  un B/P1 sur les deux lots et les retire (§11 bis, lot fautif retiré sans
  relire le reste).
- v5 (4 unités) : recomposée depuis `f444955b`. L'écart de code avec v4 est
  exactement la somme des fichiers de #3779 et de #3755.

## Admissions (4 lots, 4 unités)

| lot | source composée | avis au gel | objet |
|---|---|---|---|
| #3782 | `d206881414a0` | 2/2 (ameesh-cx2 codex #20542, auditeur claude #20543) | paquet-candidats : rendre le SHA du code exécuté et refuser un calcul fait avec un code antérieur à la cible |
| #3792 | `8951b190579e` | 1/1 (auditeur claude #20639) | Déploiement staging : retirer les anciens venv-* (courant et précédent gardés), sans toucher au lien venv-courant |
| #3788 | `b31043a0b578` | 1/1 (auditeur claude #20633) | Panier « handoff » du 2026-10-26 : défauts non bloquants (N, P2, P3) ; le chargeur de release reste exercé |
| #3735 | `62bdd562de91` | « verdict non requis » pilote 19:4xZ (union mécanique relue, 23 tests ciblés OK ; avis codex acquis sur `ddd1305f`) | Banc ameesh-pg : catalogues gonflés, purge des schémas orphelins et signal de gonflement |

## Avis au gel

| lot | gel | verdicts |
|---|---|---|
| #3782 | 20537 | #20542 ameesh-cx2 (codex) ok, #20543 auditeur (claude) ok |
| #3792 | 20637 | #20639 auditeur (claude) ok |
| #3788 | 20622 | #20633 auditeur (claude) ok |
| #3735 | 20700 | « verdict non requis » pilote (union mécanique relue) |

## Composition (4 unités, 4 lots)

| unité | lot(s) | source | commit | note |
|---|---|---|---|---|
| 1 | #3782 | `d206881414a0` | `5cd8d90a0004` | composition |
| 2 | #3792 | `8951b190579e` | `0ecb3d59fb2a` | composition |
| 3 | #3788 | `b31043a0b578` | `da4a57ceddc7` | composition |
| 4 | #3735 | `62bdd562de91` | `cb83f22a8406` | composition |

4 fusions propres, sans résolution mécanique ni adaptation de composition.
L'union de `main` dans #3735 est portée par la branche du lot, relue et gelée
par le pilote.

## Migrations et exploitation

Aucune migration : 0490 (#3755) et 0491 (#3779) sont sorties avec leurs lots.

## Écartés de h-31

| lot | motif |
|---|---|
| #3779 | retiré en v5 : B/P1 du pilote, test négatif `test_l224_session_harnais.CodexTest.test_compte_codex_secondaire` (run 40397294387, psycopg 5/8 ; reproduit sur main + #3779 seul). Va dans h-32 (#3806) une fois corrigé. |
| #3755 | retiré en v5 : B/P1 du pilote, test de contrat `test_l107_executor_contract.TableTest.test_l_interface_entiere_est_classee` (`accounts.force_switch` non classé ; run 40397294387, psycopg 2/8 ; reproduit en local). Va dans h-32 (#3806) une fois corrigé. |
| #3506 | retiré en v4 : porte illisible, provenance d'auteur inconnue pour cinq commits d'anciens gels (cas #3781). Dépend de #3781 et attend son staging. |
| #3754, #3800, #3777, #3801, #3781 | prêts dans la liste calculée de 20:3xZ, mais devenus prêts après la composition de 19:46Z ; la recomposition ne fait que retirer des lots (paquet non élargi). Ils vont dans h-32 (#3806). |
| #3519 | hors h-31 (pilote 19:4xZ : la porte refuse sa provenance de gel antérieur, cas #3781) |
| #3526 | hors h-31 (pilote 19:4xZ : provenance refusée, cas #3781 ; aucun avis sur le gel `30a916ad` et conflit avec `main`) |
| #3756 | véhicule du paquet h-30, jamais une unité |

## Contrôles

- Tests locaux v5 avant le gel : voir la note de gel du porteur (#3796).
- `scripts/freeze-checks.py` et `scripts/paquet-inclusion.py` sont rejoués sur la tête v5 (note de gel).
- Prévalidation : non bloquante, journalisée par `ameesh ci run`.
