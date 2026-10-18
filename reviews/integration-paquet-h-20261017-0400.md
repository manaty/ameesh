# Paquet integration/paquet-h-20261017-0400 — composition

Base : `main` après atterrissage de PR1571 (`integration/paquet-h-20261017-0300`),
merge réel **`38f5114054172863759debc19d6dd8c1ae575b66`** (03:47:35Z, arbre
`7f485552a344cbb0a158579e77f3464f32674faa`, identique à la tête de PR
`111e8345704c78e608f60fdd17a7eb512cceaf61`). La branche est construite sur ce
merge : son diff ne porte que les unités ci-dessous.

Exécuteur : **mty-amh-impl-01** (composition, CI et fusion). Admissions du
pilote : #366 (03:29, #94516), #2131 (03:17, tête conservée), #2139 et #2152
(03:15/03:08), #2179 (auditeur 03:04, pilote 03:08). Transports confiés par
l'auditeur : #366 (#94548, 03:45) et #2131 (tête `ef22692b`). Les courriers
demandent le manifeste du prochain paquet compatible, puis le run et la fusion
réels ; aucune CI de lot ni extension de PR1571 (atterrie).

## Composition

| unité | lot | source | commit de composition | note |
|---|---|---|---|---|
| 1 | #366 | `387f4f510fa426d7a8bc83ba1b2d41af51de5c0b` (`lot/l232d`) | `f78983316` | PR1493 ; socle OAuth/approve, avis Claude9786 + Codex9777 + DeepSeek9833 (secrets) ; fusion propre. Aucune activation/login réel : préconditions propres #366/#365 inchangées |
| 2 | #2139 | `e24011d219dd96219713971fce94696a6a9ab634` (`lot/2139-attente-reprise-deterministe`) | `d478214eb` | gel 9568, GO Claude dev4 9729 ; fusion propre |
| 3 | #2152 | `1cd0e5bd1954422221205a0203f5ec460daa89d5` (`lot/2152-drain-hors-tour`) | `dc51a0800` | GO Claude9837 + R1 Codex 36637 sur 0458/droits ; résolutions auteur 1343/1465/1476 ; fusion propre |
| 4 | #2179 | `371e8f9604382d58e9f8f54f24e22454dfbf474a` (`lot/2179-cli-roadmap`) | `ffede0a79` | PR1565 ; GO Claude9826, correction d'aide une ligne ; fusion propre (union `tests/parts/pilote-psql.txt`) |
| 5 | #2131 | `ef22692ba6a33c94ba72920c8febb799b6e1aad1` (`agent/deepseek4-2131-detach-cas`) | `22f3eed91` | gel 9734, GO Codex cx3 9735 conservé sur f02d71e5 ; tête de transport ef22692b (base main d900521b, six deltas identiques au gel) ; conflit `docs/EXECUTEUR-MEDIEE.md` seul, compteurs dérivés 65/371/436 |
| — | correction de composition | — | `e9addb9d7` | doublon `test_420_qa_role` retiré après l'union #2179 (lecture dédoublonnante, fichier propre) |

## Résolutions de composition (preuve)

- **#2131 × #1425** : `refs/ameesh/resolutions/2131/1425` (`4f10fc64`) est une
  résolution de la paire `f02d71e5` + `a2804c84`, antérieure à la tête
  `ef22692b` ; elle n'est pas utilisée comme source. La fusion de `ef22692b`
  avec `main` (qui porte déjà #1425 depuis PR1571) est **propre** hors
  `docs/EXECUTEUR-MEDIEE.md` : aucun ajustement sémantique de la résolution
  n'est requis sur cette tête.
- **#2131 × #1349** : **absent du paquet**. La résolution `52928ddb` reste
  périmée sur `docs/api/openapi-v1.json` et `ui/src/api/schema.d.ts` (radar
  03:21–03:36) ; la reprise est chez deepseek4/cx3, et le pilote a demandé de
  ne pas courir en parallèle (#94525). #1349 n'entre donc pas dans ce
  manifeste ; si un manifeste ultérieur réunit les deux lots, la paire devra
  être raccordée avant composition.
- **#2131** : conflit unique `docs/EXECUTEUR-MEDIEE.md` (compteurs). Le
  contrat composé donne 65 opérations et 371 refusées, interface dérivée 436 ;
  le doc est résolu à 65/371/436 (et non aux 368/433 de la base de la branche,
  ni aux 370/435 d'avant #2131). `test_l107_executor_contract` valide ce
  raccord, y compris l'absence d'`interface_total` dans le contrat.
- **#2179** : union additive de `tests/parts/pilote-psql.txt` ; le doublon
  `test_420_qa_role` produit par l'union est retiré (`e9addb9d7`), les deux
  modules utiles (`test_2179_roadmap_cli`, `test_420_qa_role`) restent une
  seule fois.

## Preuves locales (sur l'arbre composé)

- `tests.test_l107_executor_contract` : **22/22 OK** (compteurs 65/371/436 et
  classification de l'interface).
- `tests.test_2102_detacher_branche` (pilote psql, banc #1992) : **13/13 OK**
  — CAS, détachement concurrent, PR d'une branche détachée, radar.
- `tests.test_1270_rate_limit_retry` + `tests.test_1462_operator_pause` +
  `tests.test_2179_roadmap_cli` : **98/98 OK** (reprise/renouvellement du bail,
  pause opérateur/migration 0458, CLI roadmap).
- `tests.test_l232d_oauth` + `tests.test_l232d_credential_health` +
  `tests.test_l232d_login_e2e` : **55/55 OK** (adaptateurs épinglés, reçu,
  santé, défi réel).
- Aucune CI de lot, aucune suite complète locale ; la suite complète tourne
  une seule fois sur la PR du paquet.

## Hors périmètre (non retenus)

- **#1425** : déjà livré par PR1571 (`1e79656c1 vehicule 0300 : #1425
  (a2804c84736a)` ; `a2804c84` est ancêtre de `main` 38f51140). Le transport
  demandé par l'auditeur le 17/10 03:48 est donc **sans objet pour ce
  véhicule** ; son contenu n'est pas compté deux fois. Le rattachement de
  #1425 se fait sur le run/fusion de PR1571, pas sur ce paquet.
- **#1349** : transport frais attendu (paire 2131×1349 périmée) ; aucune
  nouvelle ref au 03:50Z.
- **#1770** : encore en build, non admis ; sa résolution `e6547973` reste au
  lot #2131, sans entrée dans ce paquet.
- **#1945/#2171**, **#753/#1467/#1465** : conditions/GO non acquis à ce
  cutoff, pas d'ajout.
- Aucun lot non admis n'est inclus ; pas d'extension de PR1571.

## CI et fusion

- Une seule CI complète sur la tête gelée de la branche (PR `pull_request`,
  suite psql+psycopg) ; aucune CI de lot.
- Fusion au vert sur la tête exacte (auto-fusion), puis rattachement des cinq
  lots aux commits réels de composition et au merge de `main`.
