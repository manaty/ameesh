# Paquet integration/paquet-h-20261016-2100 — composition

Base : `main` après atterrissage de PR1520 — merge
`cb77f8990d72e3d1a8ca0d2f54afc36775b0bbe6` (tête de contenu `2efd2816f733`) ;
branche `integration/paquet-h-20261016-2100`. Exécuteur désigné :
**mty-amh-impl-01** (composeur, CI et fusion). Gels et avis des lots conservés
(contenus constants, §11bis/§12) ; aucune nouvelle relecture. Aucune CI de lot.

La colonne « commit de composition » porte la preuve d'inclusion : chaque lot a
son commit dans la branche (`git log --first-parent`).

## Admissions retenues à la coupure

#2022, #2132, #1566, #2134, #1901, #811, #1669, #2119, #2045, #1648, #1132,
#2128/#2127, #1374, #917, #2102
(voir « Déjà atterrie » pour #2023).

## Composition

| unité | lot | source | commit de composition | note |
|---|---|---|---|---|
| 1 | #2022 | `bf156ce347fb` | `205b326d0` | Raccord dev5 (2be7efec × gel d95f227bbac4), conflit unique `docs/EXECUTEUR-MEDIEE.md` résolu par les compteurs du contrat fusionné — ameesh-dev5, avis ameesh-cx2 et mty-amh-docs-01 |
| 2 | #2132 | `132ddd7799a5` | `a3b13e6d9` | #2130 D projection opt-in des lots pinnés Process — mty-amh-impl-02, verdict mesh-design (GO9504) |
| 3 | #1566 | `b06a710acb19` | `29a753ad7` | Remise ancrée sur main (arbre = gel `12c2f24f82ed`) — ameesh-dev3, qualification 9513, verdicts ameesh-dev4 et pilote |
| 4 | #2134 | `0ffc170b7da4` | `8873884b2` | CI : le portier paquet accorde `actions: read` au reusable tests.yml — deepseek4, avis Codex (mesh-design) et Claude (orchestrateur) |
| 5 | #1901 | `a6b478a025f0` | `6946bc60d` | N3 de la relecture Claude de #1463 : « absence ≠ résolution » sur une famille d'alertes non lue (resolve once, `_KEPT`, accusé non résolu) — avis Claude, transport confié par l'orchestrateur (note 36061) |
| 6 | #811 | `21bf81bea` (remise v2) | `3f6c829a4` | `_pick` ne dépend plus de `cfg` (doublures partielles, #1270) ; lot secondaire sans dossier : pas de reprise dans la session du lot précédent — impl04, lecture bornée Codex/auditeur (aucun B), réadmission pilote 21:25 |
| 7 | #1669 | `7f6854b0` (remise qa01) | `fa9228acd` | Delta seul (correctif cx1 `--root/--onto`) sur main `cb77f899` — mty-amh-qa-01, admission LIGHT et GO9095, 25 ciblés verts, aucune dépendance #1476 |
| 8 | #2119 | `25c9304c` via résolution `2022/2119` = `9b174064` | `fd672a060` | Garde de relance hébergée couvre aussi le portier paquet — deepseek4, GO Codex ; conflit de repliage `docs/EXECUTEUR-MEDIEE.md` résolu en gardant les compteurs du véhicule |
| 9 | #2045 | `db0dd77c` | `b2557da12` | Doc : preuve de contenu du raccourci land « déjà landed » — deepseek4, GO Codex docs ; doc seule |
| 10 | #1648 | `b49531c4` | `3463d6af3` | Jauges du widget préférées à un journal plus frais — admission pilote/auditeur post-h1900 (21:55), anciennes têtes 2012007d / union 1286 superseded |
| 11 | #1132 | `1db73514` (remise impl05) | `22c475eab` | Jauges périmées, union #1132 × #1661, 201 ciblés verts psql + psycopg — admission pilote confirmée 21:56 (gel 30bb9f38), contrat dérivé |
| 12 | #2128/#2127 | `a980bcfc` (tête commune) | `cf82add86` | Un registre de disponibilité illisible ne s'écrase pas et se dit — lot unique admis après #1901 (GO9530/9529), adapte `test_1901_gate_unreadable` |
| 13 | #1374 | `e5c6249b` | `d3da58e81` | Feu auto-fusion, clé de cible canonique (union #1374 × #1680/#1703) — admission pilote confirmée 21:55, PR1126, aucun exécutant isolé |
| 14 | #917 | `b33c1e25` (remise impl05) | `ec716cb81` | Docstrings/projections, `_projects` + repli lot_team — admission conservée 21:58, 177+177 ciblés verts (gel 4b59d47c, GO dev1 #6826) |
| 15 | #2102 | `17ae2d2d` | `3db86c1fa` | Association implicite atomique, un détachement pendant le relevé n'est pas perdu — admission 21:28 (PR1517) ; conflits de compteurs `docs/EXECUTEUR-MEDIEE.md` résolus aux valeurs du contrat fusionné |

## Déjà atterrie (aucune composition)

- **#2023** : tête `66e8ce1a` (doc DSN par défaut) est déjà ancêtre de
  `cb77f899` (composée au paquet h-1900, `96a0e7018`) — rien à transporter ;
  l'ancienne tête `6f887b4f` citée par la note auteur est superseded.

## Retirés de ce véhicule

Aucun. Rappel : #811 avait été retiré du paquet 1700 r2 sur une régression
attribuée à tort (cause réelle #1270 `TourDeRepriseTest` appelant `_pick` sur un
`__new__` sans `cfg`) ; la remise v2 corrige cette cause et est reprise ici.
Hors véhicule, faute de tête ou d'admission reçue : #2120 (résolution
`2120/1467` publiée par dev5 pour le radar, aucune admission nommée ici), #508
(paires L160d en cours, pas de tête qualifiée).

## Contrôles de composition

- Union sur `cb77f899` : rejeu mécanique des six unités déjà préparées sur la
  tête de contenu `2efd2816`, puis neuf unions ; aucun marqueur, `git diff
  --check` propre, migration `0455_host_processes` toujours unique.
- Conflits résolus : #811 (deux hunks, résolution enregistrée du paquet 1700
  adaptée à #1898 N3) ; #2119 (un hunk de repliage) ; #2102 (deux hunks de
  compteurs) — les compteurs finaux viennent du contrat fusionné.
- **Piège à compteurs** : #2022, #1132 et #2102 refusent chacun une opération de
  plus que `main` (364 → 367 refusées, 428 → 431 à l'interface) ; le doc
  `docs/EXECUTEUR-MEDIEE.md` a été aligné sur les valeurs du contrat fusionné
  (**64 admises / 367 refusées / 431 interface**) et `test_l107` le vérifie.
- Témoins ciblés sur l'arbre d'union final (`3db86c1fa`), psql + psycopg :
  **514 OK + 514 OK** — 20 modules : `l107` (compteurs), `l122` (migrations),
  `2128_registre_illisible`, `1901_gate_unreadable`, `1374_fire`,
  `1003_merge_pr`, `1680_packet_reservation`, `1703_writer_identity`,
  `l160e_merge_queue`, `l132_alert_briefs`, `l26_exploitation`,
  `2102_detacher_branche`, `1132_jauges_perimees`, `1580_widget_forfait`,
  `1277_rebase_sujet`, `987_relance_hosted`, `811_lot_workdirs`,
  `2130_d_projection`, `2022_host_zombies`, `2022_fleet`.
  Runs antérieurs sur la même union : 112+112 (#2132/`l107`/`l122`), 109+109
  (#2022/`l43`/`l1566`/`cluster`), 63+63 (#2134/`1410`/`1644`/`l756`), 85+85
  (#1901), 198+198 (#811 — un échec psql du flaky #2139, vert en psycopg).
- Contrôle d'inclusion `scripts/paquet-inclusion.py` : **14 présents, 2 entrées à
  lire et expliquées**, aucune omission.
  - entrée 1 **#2022 « absent »** : le script ne voit plus que 2 lignes sur les
    1 102 ajoutées — les deux lignes de compteurs de `docs/EXECUTEUR-MEDIEE.md`,
    réécrites par les unions ultérieures (#1132 puis #2102 refusent chacun une
    opération de plus). Preuve de substance à la tête : `hosts.runners` (seule
    opération refusée ajoutée par #2022) est bien dans la liste refusée du
    contrat (367), migration `0455_host_processes.sql` unique, `resources.py`,
    `mesh_cli.py`, `notify.py`, `hosts.py` et les trois témoins #2022 présents et
    verts. Aucune omission, aucun retrait à consigner.
  - entrée 5 **#1901 « partiel »** : les lignes absentes sont l'adaptation
    apportée par #2128/#2127 (`_gate_alerts(..., unreadable=…)`,
    `availability.local_alerts`, témoin adapté) ; le contenu #1901 est bien à la
    tête sous cette forme, `test_1901_gate_unreadable` et
    `test_2128_registre_illisible` le vérifient.
  - Verdict du script : 1 défaut classé « absent » pour #2022 — lu et expliqué
    ci-dessus, pas une omission ; les deux entrées sont signalées au pilote au
    gel.
- Flaky connu, distinct du véhicule : `test_attente_apres_echec_renouvelle_le_bail`
  (synchronisation d'ordonnancement) se reproduit sur `main` sans #811 ; lot
  #2139 créé et assigné à impl04 (correction à la source). À ne pas confondre
  avec un défaut de l'union si la suite complète le rencontre.
- Preuve runtime du portier (#2134) : mesh-design (21:13Z) retient l'option (a) —
  flux PR habituel, une seule suite complète portée par le run PR ; pas de tête
  de preuve dédiée. Un succès du portier avec `suite` sautée ne vaut pas preuve :
  celle-ci reste due au prochain événement `integration/**` sans PR couvrante.
- **Une seule CI complète** sur la tête gelée (§12) ; aucune CI de lot.
