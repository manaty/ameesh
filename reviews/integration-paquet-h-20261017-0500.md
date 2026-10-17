# Paquet integration/paquet-h-20261017-0500 — composition

Base : `main` après atterrissage de PR1577 (`integration/paquet-h-20261017-0400`),
merge réel **`18859e0664c291f199bc88b8c98abd7e68b1222f`** (04:49:02Z, arbre identique à la tête
`f2be9d0f4fca990afa54f8ed03084a459b699352`). La branche est construite sur
`f2be9d0f4`, ancêtre de `main` après cette fusion : le diff de la PR ne porte
que les unités ci-dessous.

Exécuteur : **mty-amh-impl-01** (composition, CI et fusion). Admissions du
pilote : #2190 (04:15, GO final auditeur sur `62b37ebd7`), #1677 (admission
maintenue au prochain paquet compatible, remise `91df05dac` sur la tête 0400),
#2189 (04:26, R1 Codex favorable sur `e583d234c`), #2177 (04:29, GO Codex sur
`4ff178510`), #2157 (04:29, R1 Codex favorable sur `1ee9af5c`, « admis
prochain paquet compatible »). Transports : #2190 confié par mesh-design
(#94841/#94861) ; #1677 remise poussée par mty-amh-impl-07 ; #1152/#1343
signalés prêts par mty-amh-impl-04 mais différés (voir Hors périmètre). Aucune
CI de lot.

## Composition

| unité | lot | source | commit de composition | note |
|---|---|---|---|---|
| 1 | #2190 | `62b37ebd701d082368384e10d6c6d7945388dc14` (`lot/2190-sonde-libre-rapport-ko`) | `28eba5c91` | priorité 1, staging ; gel 04:05, GO auditeur 04:14 sur la même tête ; fusion propre. 4 fichiers, zéro migration |
| 2 | #2189 | `e583d234ca1d38acd2fd66f6d4145d1d5c400f55` (`lot/2189-rejeu-attributs-cible`) | `6687ee360` | R1 Codex (mesh-design) 04:26, correction sur la branche ; fusion propre (`resolutions.py`, `test_l160d_resolutions.py`) |
| 3 | #2177 | `4ff178510492b742259218892432fa1a0358fbbb` (`lot/2177-carte-fichier-vide`) | `497cd68f6` | GO Codex (mesh-design) 04:29 ; fusion propre (`tests/fake_acp_agent.py`, nouveau témoin SIGTERM) |
| 4 | #2157 | `1ee9af5c10e662176a43067e76324bd4fb5ebfed` (`lot/2157-composition-ameesh-work`) | `e26883b43` | R1 Codex favorable 04:29, léger ; outil de composition + convention `ameesh-work` ; fusion propre |
| 5 | #1677 | `5b208e277d16` (tête gelée ; remise `91df05dac`, `refs/ameesh/resolutions/1677/paquet-h-20261017-0400`) | `fa961a70a` | priorité 1, prise automatique ; GO dev7 7023 conservé, remise fraîche sur la tête 0400 ; fusion propre, 16 fichiers |

Chaque commit de composition porte le trailer `ameesh-work: <lot>` (convention
#2157) ; les commits de fusion portent le lot comme second parent.

## Résolutions de composition (preuve)

- Aucun conflit : les cinq unités touchent des fichiers disjoints sur la base
  `f2be9d0f4` (deploy staging, `resolutions.py`, témoins de mesure, outillage
  de composition, prise automatique). Aucune résolution manuelle, aucun delta
  sémantique.
- #2190 est la première unité (priorité 1) ; aucun code relu de #2190 n'est
  modifié par la composition : ses trois fichiers propres sont identiques à
  `62b37ebd7`, et `docs/DEPLOIEMENT-HOTE-AGENTS.md` ne reçoit en plus que la
  section #2152 apportée par la base 0400 (hunks disjoints, passages #2190
  intacts).
- #1677 : la remise `91df05dac` (parents `f2be9d0f4` + `2b5ef81f`) porte le
  delta #1677 seul (16 fichiers, +955/−61) ; sa fusion sur le paquet est
  propre et ne réintroduit aucun contenu du 0400.
- `git diff f2be9d0f4..HEAD` = 28 fichiers, +2334/−182, exactement la somme
  des cinq deltas ; `git diff --check` propre.

## Preuves locales (arbre composé)

- `tests.test_1155_staging` + `tests.test_1323_actif` (cibles #2190) :
  **113 OK**.
- `tests.test_l160d_resolutions` (cible #2189) : **53 OK**.
- `tests.test_2177_couverture_sigterm` (cible #2177) : **2 OK**.
- `tests.test_2157_paquet_compose` (cible #2157) : **5 OK**.
- `tests.test_1677_prise_empilee` + `tests.test_l119_backlog` +
  `tests.test_l234_auditor` (cible #1677, banc psql 55432) : **63 OK**.
- Total **236 tests ciblés OK**, `bash -n` sur `staging.sh`, `py_compile` sur
  `sante-courrier.py` et `paquet-compose.py`, `diff --check` propre.
- Aucune CI de lot, aucune suite complète locale : une seule CI complète
  tourne sur la tête de la PR du paquet (§12).

## Hors périmètre (différé, non retenu à ce cutoff)

- **#1152** : la résolution publiée `refs/ameesh/resolutions/1152/2190` =
  `8fd29d790797` (parents `a74d8fd64` + `62b37ebd`, delta vs `62b37ebd` =
  delta #1152 exact 10 fichiers +550/−233, 156 ciblés OK) est prête, mais sa
  composition sur ce paquet rouvre un conflit `deploy/mise-a-jour/vm.sh` avec
  #2152 (drain hors tour) et un delta de composition à signaler. Elle passe
  **après** #2190, selon la consigne de mty-amh-impl-04 (« la résolution est
  prête pour quand #2190 aura atterri ») ; elle sera reprise au véhicule
  suivant, sur la base à jour.
- **#1343** : admission/transport confirmés, tête `08f384f028` inchangée ;
  la remise disponible (`662647761`, base `ac7bfdb23` du 16/10) demande le
  rejeu des résolutions #379/#1359/#1466/#2152 et du correctif de parts. Elle
  demande un véhicule dédié avec mty-amh-impl-04, pas ce cutoff prioritaire.
- **#2186** : admission conditionnée à #2043, absent de `main`.
- **#1114** : précondition de configuration staging toujours ouverte.
- **#1465/#1612** : prêts (gel/verdict ok, remises `fb9a2575`/`70c9afa3`) mais reportés au véhicule suivant ; composition de banc locale `wip/paquet-0400-7units`, non poussée.

## CI et fusion

- Une seule CI complète sur la tête gelée de la branche (PR, suite
  psql+psycopg) ; aucune CI de lot.
- Fusion automatique au vert sur la tête exacte (`--match-head-commit`) ;
  ne pas rebaser.
- #2190 et #1677 sont reliés à ce manifeste et au run de la PR ; après
  fusion, rattachement des cinq lots aux commits réels et confirmation à
  mesh-design. L'activation staging du correctif #2190 reste coordonnée par
  bureau après intégration ; aucune opération hôte n'est faite ici.
