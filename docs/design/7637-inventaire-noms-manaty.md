# Noms Manaty du cœur — inventaire et garde (#7637, tranche 1)

> **Lot** : #7660, tranche 1 de #7637 (spec §5, référence #7627).
> **Date** : 17/11/2026. **Base** : `main` `aeca7e2ebca4994d477cc9e2a7cb2970a4fdf007`.
> **Portée** : `src/ameesh/`, hors `modules/`, tests et notes. Aucun changement de comportement.
> **Garde** : `tests/test_7660_coeur_sans_noms_manaty.py`. Ce document et le test portent la même liste ; toute évolution passe par les deux.

## 1. Ce qui est compté

Un **nom Manaty** est un nom propre que le cœur ne peut pas connaître d'un mesh à l'autre : un rôle de gouvernance, un hôte, une personne ou un projet. Le canon les porte (spec §5, « noms des rôles, hôtes, personnes, processus, routes d'alerte ») ; le cœur les lit, il ne les écrit pas.

Le lexique est fermé et explicite :

| Famille | Noms (formes reconnues) | Occurrences | Fichiers | Tranche |
|---|---|---|---|---|
| Rôle | `mesh-design`, `auditeur`, `orchestrateur`, `bureau` | 189 | 41 | #7661, #7634 |
| Hôte | `ameesh-agents-1`, `ameesh-agents-2`, `ameesh-agents-3`, `manaty-mesh-1`, `pc-smichea`, `bureau-goal` | 9 | 4 | #7662 |
| Personne | `smichea` | 5 | 3 | #7662 |
| Projet | `nexlink`, `manaty` | 237 | 19 | #7663, — (aucune) |
| **Total** | | **440** | **56** | |

Les formes plurielles (`auditeurs`, `orchestrateurs`, `bureaux`) sont comptées
sous le nom canonique. Les noms les plus longs sont masqués d'abord :
`pc-smichea` est un hôte, pas l'hôte + la personne `smichea` ; `bureau-goal`
est un hôte, pas le rôle `bureau` ; `manaty-mesh-1` est un hôte, pas
l'organisation `manaty`.

### Ce qui n'est pas compté, et pourquoi

- **`ameesh`** : nom du produit, du paquet, de la commande et de la base ;
  `ameesh-agents.slice` est le préfixe de slice cgroup des exécuteurs, pas un
  hôte. Ce n'est pas un nom de client à externaliser.
- **`pilote`, `relecteur`, `composeur`, `veille`**, et les alias anglais
  `pilot`, `review`, `auditor`, `orchestrator` : vocabulaire générique
  d'interface, pas des identités de fiche. Le canon Manaty les emploie, mais le
  cœur peut les afficher sans connaître un mesh. Les quatre rôles retenus sont
  ceux que le lot cite comme identités : `mesh-design`, `auditeur`, `bureau`,
  `orchestrateur`.
- **`modules/`** : un module porte son nom et son vocabulaire. C'est la destination de la
  tranche 4.
- **Tests, docstrings, commentaires, notes, `COMMENT ON` SQL** : le corps du lot exclut les
  notes. Le scan lit le code exécutable, pas les explications.
- **`deploy/`, `scripts/`, `docs/`, `.json`, `.css`, `.md`** : hors du périmètre `src/ameesh/`
  ou non exécutables.

## 2. Méthode de recherche

Le scan est celui de `tests/test_7660_coeur_sans_noms_manaty.py` ; l'inventaire et la garde ne peuvent pas diverger.

1. Fichiers : `src/ameesh/**` de suffixe `.py`, `.sql`, `.html`, `.js`, `.cjs`, sauf
   `src/ameesh/modules/**`.
2. Python : `ast.parse` ; sont lus les littéraux de chaîne hors docstrings,
   les identifiants (`Name`, `Attribute`, `arg`, `keyword`), les noms de
   fonctions/classes et les imports (module et alias). Un littéral multi-ligne
   qui ressemble à du SQL voit ses commentaires `--` retirés avant lecture. La
   ligne est celle du littéral, décalage des multi-lignes compris.
3. SQL : commentaires `--`, `/* */` et instructions `COMMENT ON …;` retirés (notes de base) ;
   les littéraux et le code des triggers restent.
4. JS/HTML : commentaires `//`, `/* */`, `<!-- -->` retirés ; dans le HTML, seuls les blocs
   `<script>` sont traités comme du JS.
5. Correspondance insensible à la casse, frontières alphanumériques, pluriels,
   masquage des noms les plus longs. Comptage par `(fichier, nom canonique)` ;
   les lignes servent à l'inventaire, pas au comptage.

Reproduction :

```sh
PYTHONPATH=src python3 -m unittest tests.test_7660_coeur_sans_noms_manaty -v
```

La fonction `occurrences()` du test rend les 440 lignes de l'inventaire ; `EXCEPTIONS` porte le même contenu agrégé par fichier et nom.

## 3. Synthèse par tranche

| Tranche | Occurrences | Fichiers |
|---|---|---|
| #7634 | 135 | 26 |
| #7661 | 54 | 22 |
| #7662 | 14 | 6 |
| #7663 | 228 | 17 |
| — (aucune) | 9 | 4 |
| **Total** | **440** | **56** |

Un fichier peut porter plusieurs tranches (ou plusieurs familles) : les colonnes « Fichiers » ne s'additionnent pas, le total est le nombre de fichiers distincts.

- **#7634** : le nom `bureau` sort du cœur (routage humain par l'avatar). Il en
  reste 3 occurrences SQL dans des migrations : elles sont figées dans
  l'histoire du schéma, pas réécrites ; la tranche doit soit ajouter une
  migration, soit les assumer et laisser la liste `#7634` à 3 entrées.
- **#7661** : les rôles `mesh-design`, `auditeur`, `orchestrateur` sont lus à la fiche du canon.
- **#7662** : les hôtes et la personne `smichea` sont lus au canon.
- **#7663** : le module Nexlink reprend la release, le train et le canal d'alertes `nexlink`.
- **Aucune tranche** : `manaty` (organisation et dépôts `manaty/ameesh`,
  `manaty/nexlink`, `manaty/home`) n'est couvert par aucun des quatre lots.
  C'est un constat à trancher par le pilote.

## 4. Inventaire

Pour chaque fichier : les lignes, le nom, sa nature, sa forme dans le code et
la tranche qui la retire. Une même ligne peut apparaître sur deux lignes de
tableau si elle porte deux formes différentes ; `×N` signale N occurrences du
même nom sur la même ligne.

### `alert_briefs.py` (67 occurrences)

| Ligne(s) | Nom | Nature | Forme | Tranche |
|---|---|---|---|---|
| 116, 118, 1300, 1366, 1376 | `auditeur` | rôle | constante/clé | #7661 |
| 264, 270, 277, 1291, 1296, 1298, 1299 ×2, 1303, 1342, 1385, 1392, 1430, 1495, 1588, 1696 | `bureau` | rôle | identifiant | #7634 |
| 267, 275, 278, 279, 1291, 1296, 1297, 1298, 1299, 1303, 1341, 1343, 1366, 1374, 1388, 1398, 1399, 1440, 1441, 1499, 1500, 1706, 1707 | `bureau` | rôle | constante/clé | #7634 |
| 537, 686 ×2, 691, 701, 724, 746, 757, 760, 785, 798, 821, 831, 846, 870, 899, 917, 932, 948 | `bureau` | rôle | message | #7634 |
| 686, 691 | `manaty` | projet | message | — (aucune) |
| 1494 ×2 | `mesh-design` | rôle | constante/clé | #7661 |

### `approve/config.py` (2 occurrences)

| Ligne(s) | Nom | Nature | Forme | Tranche |
|---|---|---|---|---|
| 85, 86 | `nexlink` | projet | constante/clé | #7663 |

### `auditor_pass.py` (2 occurrences)

| Ligne(s) | Nom | Nature | Forme | Tranche |
|---|---|---|---|---|
| 51 | `auditeur` | rôle | constante/clé | #7661 |
| 337 | `auditeur` | rôle | message | #7661 |

### `backlog.py` (4 occurrences)

| Ligne(s) | Nom | Nature | Forme | Tranche |
|---|---|---|---|---|
| 770 ×2, 776 | `auditeur` | rôle | constante/clé | #7661 |
| 1782 | `orchestrateur` | rôle | message | #7661 |

### `canon.py` (5 occurrences)

| Ligne(s) | Nom | Nature | Forme | Tranche |
|---|---|---|---|---|
| 2008, 2077, 4310, 4791 | `bureau` | rôle | message | #7634 |
| 4630 | `mesh-design` | rôle | message | #7661 |

### `ci.py` (2 occurrences)

| Ligne(s) | Nom | Nature | Forme | Tranche |
|---|---|---|---|---|
| 3360 | `nexlink` | projet | message | #7663 |
| 3778 | `nexlink` | projet | constante/clé | #7663 |

### `ci_disk.py` (1 occurrence)

| Ligne(s) | Nom | Nature | Forme | Tranche |
|---|---|---|---|---|
| 171 | `bureau` | rôle | message | #7634 |

### `cli.py` (9 occurrences)

| Ligne(s) | Nom | Nature | Forme | Tranche |
|---|---|---|---|---|
| 2465, 2468 ×3, 2469 ×2, 2471, 2472, 2717 | `nexlink` | projet | identifiant | #7663 |

### `context_profile.py` (2 occurrences)

| Ligne(s) | Nom | Nature | Forme | Tranche |
|---|---|---|---|---|
| 104 | `orchestrateur` | rôle | constante/clé | #7661 |
| 112 | `auditeur` | rôle | constante/clé | #7661 |

### `cost.py` (1 occurrence)

| Ligne(s) | Nom | Nature | Forme | Tranche |
|---|---|---|---|---|
| 1196 | `nexlink` | projet | constante/clé | #7663 |

### `cycle_budget.py` (3 occurrences)

| Ligne(s) | Nom | Nature | Forme | Tranche |
|---|---|---|---|---|
| 98 | `bureau` | rôle | constante/clé | #7634 |
| 99, 195 | `bureau` | rôle | message | #7634 |

### `debit.py` (7 occurrences)

| Ligne(s) | Nom | Nature | Forme | Tranche |
|---|---|---|---|---|
| 190, 664, 3439 ×2 | `bureau` | rôle | constante/clé | #7634 |
| 190, 1420 | `bureau` | rôle | message | #7634 |
| 2586 | `auditeur` | rôle | message | #7661 |

### `decisions.py` (6 occurrences)

| Ligne(s) | Nom | Nature | Forme | Tranche |
|---|---|---|---|---|
| 129 | `bureau` | rôle | constante/clé | #7634 |
| 936, 968, 970 | `bureau` | rôle | identifiant | #7634 |
| 937, 938 | `bureau` | rôle | message | #7634 |

### `deep_analysis.py` (1 occurrence)

| Ligne(s) | Nom | Nature | Forme | Tranche |
|---|---|---|---|---|
| 201 | `auditeur` | rôle | message | #7661 |

### `exploitation.py` (4 occurrences)

| Ligne(s) | Nom | Nature | Forme | Tranche |
|---|---|---|---|---|
| 1026, 1027 | `bureau` | rôle | constante/clé | #7634 |
| 2088 | `auditeur` | rôle | message | #7661 |
| 2088 | `bureau` | rôle | message | #7634 |

### `file_decoupage.py` (2 occurrences)

| Ligne(s) | Nom | Nature | Forme | Tranche |
|---|---|---|---|---|
| 89, 429 | `auditeur` | rôle | message | #7661 |

### `fingerprints.py` (1 occurrence)

| Ligne(s) | Nom | Nature | Forme | Tranche |
|---|---|---|---|---|
| 58 | `nexlink` | projet | constante/clé | #7663 |

### `handoff.py` (2 occurrences)

| Ligne(s) | Nom | Nature | Forme | Tranche |
|---|---|---|---|---|
| 291, 300 | `orchestrateur` | rôle | message | #7661 |

### `host_cli.py` (2 occurrences)

| Ligne(s) | Nom | Nature | Forme | Tranche |
|---|---|---|---|---|
| 272, 508 | `nexlink` | projet | message | #7663 |

### `hostcheck.py` (3 occurrences)

| Ligne(s) | Nom | Nature | Forme | Tranche |
|---|---|---|---|---|
| 493 | `bureau` | rôle | message | #7634 |
| 546, 549 | `mesh-design` | rôle | message | #7661 |

### `incident.py` (4 occurrences)

| Ligne(s) | Nom | Nature | Forme | Tranche |
|---|---|---|---|---|
| 762, 763 | `manaty` | projet | constante/clé | — (aucune) |
| 763 ×2 | `nexlink` | projet | constante/clé | #7663 |

### `mesh_cli.py` (1 occurrence)

| Ligne(s) | Nom | Nature | Forme | Tranche |
|---|---|---|---|---|
| 4840 | `bureau` | rôle | message | #7634 |

### `migrations.py` (5 occurrences)

| Ligne(s) | Nom | Nature | Forme | Tranche |
|---|---|---|---|---|
| 99, 128 ×2 | `manaty` | projet | constante/clé | — (aucune) |
| 128 ×2 | `nexlink` | projet | constante/clé | #7663 |

### `migrations/0364_key_holders.sql` (2 occurrences)

| Ligne(s) | Nom | Nature | Forme | Tranche |
|---|---|---|---|---|
| 381 ×2 | `smichea` | personne | SQL | #7662 |

### `migrations/0583_bureau_staging_catchup.sql` (1 occurrence)

| Ligne(s) | Nom | Nature | Forme | Tranche |
|---|---|---|---|---|
| 158 | `bureau` | rôle | SQL | #7634 |

### `migrations/0584_bureau_catchup_timestamp.sql` (1 occurrence)

| Ligne(s) | Nom | Nature | Forme | Tranche |
|---|---|---|---|---|
| 145 | `bureau` | rôle | SQL | #7634 |

### `migrations/rollback/0584_restore.sql` (1 occurrence)

| Ligne(s) | Nom | Nature | Forme | Tranche |
|---|---|---|---|---|
| 144 | `bureau` | rôle | SQL | #7634 |

### `netcheck.py` (1 occurrence)

| Ligne(s) | Nom | Nature | Forme | Tranche |
|---|---|---|---|---|
| 46 | `bureau` | rôle | message | #7634 |

### `nexlink_release.py` (51 occurrences)

| Ligne(s) | Nom | Nature | Forme | Tranche |
|---|---|---|---|---|
| 33 ×3, 56, 404, 634, 987, 1102, 1107, 1145, 1415, 1468, 1560, 1625 | `nexlink` | projet | constante/clé | #7663 |
| 54, 59, 64, 78, 81, 149, 281, 285, 382, 431, 442, 444, 465, 478, 524, 618, 623, 994, 998, 1103, 1109, 1114, 1118, 1123, 1127, 1180, 1232, 1337, 1364, 1380, 1389, 1418, 1487, 1527, 1530, 1633, 1667 | `nexlink` | projet | message | #7663 |

### `notify.py` (83 occurrences)

| Ligne(s) | Nom | Nature | Forme | Tranche |
|---|---|---|---|---|
| 128 | `nexlink` | projet | import | #7663 |
| 265, 300, 639, 732, 752, 1112, 1135, 1176, 1202, 1235, 1377, 3963, 4460, 4814 | `nexlink` | projet | constante/clé | #7663 |
| 743, 747, 1006, 1116, 1118, 1137, 1192, 1202, 1214, 1237, 1322, 1370, 1375, 1378, 1386, 1393, 1394, 1395, 1414, 1417, 1419, 1421, 1425, 1440, 1455, 1458, 1464, 1473, 1474, 1476, 1480, 1483, 1485, 1488, 1489, 1494, 1495, 1498, 1503, 1504, 1505, 1513, 1522, 1523, 1524, 1526, 1530, 1532, 1533, 1535, 1561, 2309, 3934, 3964, 3992, 4354, 4464 | `nexlink` | projet | identifiant | #7663 |
| 745, 755 ×2, 1382, 1492, 4044 | `nexlink` | projet | message | #7663 |
| 3609, 3611, 3618, 3713, 3717 | `bureau` | rôle | constante/clé | #7634 |

### `notify_gap.py` (2 occurrences)

| Ligne(s) | Nom | Nature | Forme | Tranche |
|---|---|---|---|---|
| 319, 324 | `bureau` | rôle | constante/clé | #7634 |

### `packet_guard.py` (3 occurrences)

| Ligne(s) | Nom | Nature | Forme | Tranche |
|---|---|---|---|---|
| 259 | `bureau` | rôle | message | #7634 |
| 265, 276 | `manaty` | projet | message | — (aucune) |

### `planned_waits.py` (6 occurrences)

| Ligne(s) | Nom | Nature | Forme | Tranche |
|---|---|---|---|---|
| 263, 270, 272, 282 ×2 | `bureau` | rôle | constante/clé | #7634 |
| 330 | `bureau` | rôle | message | #7634 |

### `platform/child_environment.py` (1 occurrence)

| Ligne(s) | Nom | Nature | Forme | Tranche |
|---|---|---|---|---|
| 42 | `auditeur` | rôle | constante/clé | #7661 |

### `process_observations_cli.py` (1 occurrence)

| Ligne(s) | Nom | Nature | Forme | Tranche |
|---|---|---|---|---|
| 224 | `bureau-goal` | hôte | message | #7662 |

### `release.py` (36 occurrences)

| Ligne(s) | Nom | Nature | Forme | Tranche |
|---|---|---|---|---|
| 241, 266, 369, 473, 474, 979, 984, 1194, 1212 | `nexlink` | projet | constante/clé | #7663 |
| 277, 475 ×2, 482, 986, 1003, 1191, 1208 | `nexlink` | projet | message | #7663 |
| 765 | `bureau` | rôle | constante/clé | #7634 |
| 769, 771 ×2, 788, 792, 1152, 1175, 1187 | `bureau` | rôle | message | #7634 |
| 856, 974 | `nexlink` | projet | import | #7663 |
| 860, 868, 881, 883, 884, 885, 992 | `nexlink` | projet | identifiant | #7663 |
| 1179 | `manaty-mesh-1` | hôte | constante/clé | #7662 |

### `release_cutoff.py` (26 occurrences)

| Ligne(s) | Nom | Nature | Forme | Tranche |
|---|---|---|---|---|
| 121 | `ameesh-agents-1` | hôte | constante/clé | #7662 |
| 121 | `ameesh-agents-2` | hôte | constante/clé | #7662 |
| 121 | `ameesh-agents-3` | hôte | constante/clé | #7662 |
| 122 | `manaty-mesh-1` | hôte | constante/clé | #7662 |
| 123 | `pc-smichea` | hôte | constante/clé | #7662 |
| 1208, 1253 | `smichea` | personne | constante/clé | #7662 |
| 1255, 1257 ×2, 1262, 1274, 1350, 1717, 3121 | `nexlink` | projet | constante/clé | #7663 |
| 1256, 1259 ×2, 1264 | `nexlink` | projet | message | #7663 |
| 1353, 1718, 3122 | `nexlink` | projet | import | #7663 |
| 1354, 1719, 3123 | `nexlink` | projet | identifiant | #7663 |
| 2880 | `mesh-design` | rôle | constante/clé | #7661 |

### `review_designation_activation.py` (1 occurrence)

| Ligne(s) | Nom | Nature | Forme | Tranche |
|---|---|---|---|---|
| 5669 | `mesh-design` | rôle | message | #7661 |

### `role.py` (1 occurrence)

| Ligne(s) | Nom | Nature | Forme | Tranche |
|---|---|---|---|---|
| 41 | `bureau` | rôle | message | #7634 |

### `role_timers.py` (6 occurrences)

| Ligne(s) | Nom | Nature | Forme | Tranche |
|---|---|---|---|---|
| 118 | `mesh-design` | rôle | constante/clé | #7661 |
| 178, 179, 180 | `auditeur` | rôle | constante/clé | #7661 |
| 184 | `auditeur` | rôle | message | #7661 |
| 557 | `nexlink` | projet | message | #7663 |

### `runner.py` (6 occurrences)

| Ligne(s) | Nom | Nature | Forme | Tranche |
|---|---|---|---|---|
| 9927, 9928, 9939, 17368, 17374 | `auditeur` | rôle | identifiant | #7661 |
| 17368 | `auditeur` | rôle | constante/clé | #7661 |

### `server_faults.py` (1 occurrence)

| Ligne(s) | Nom | Nature | Forme | Tranche |
|---|---|---|---|---|
| 51 | `nexlink` | projet | constante/clé | #7663 |

### `slo.py` (4 occurrences)

| Ligne(s) | Nom | Nature | Forme | Tranche |
|---|---|---|---|---|
| 128, 132 | `auditeur` | rôle | constante/clé | #7661 |
| 128, 132 | `bureau` | rôle | constante/clé | #7634 |

### `sous_utilisation.py` (6 occurrences)

| Ligne(s) | Nom | Nature | Forme | Tranche |
|---|---|---|---|---|
| 119, 125, 127 | `orchestrateur` | rôle | constante/clé | #7661 |
| 125, 128, 861 | `auditeur` | rôle | constante/clé | #7661 |

### `staging_deferred.py` (4 occurrences)

| Ligne(s) | Nom | Nature | Forme | Tranche |
|---|---|---|---|---|
| 75 | `bureau` | rôle | constante/clé | #7634 |
| 524, 540, 550 | `bureau` | rôle | message | #7634 |

### `staging_executor.py` (4 occurrences)

| Ligne(s) | Nom | Nature | Forme | Tranche |
|---|---|---|---|---|
| 454 | `manaty-mesh-1` | hôte | constante/clé | #7662 |
| 483 | `pc-smichea` | hôte | constante/clé | #7662 |
| 1426, 1598 | `mesh-design` | rôle | constante/clé | #7661 |

### `staging_pending.py` (1 occurrence)

| Ligne(s) | Nom | Nature | Forme | Tranche |
|---|---|---|---|---|
| 402 | `bureau` | rôle | constante/clé | #7634 |

### `stagnation.py` (1 occurrence)

| Ligne(s) | Nom | Nature | Forme | Tranche |
|---|---|---|---|---|
| 182 | `bureau` | rôle | constante/clé | #7634 |

### `storage/postgres/authority.py` (1 occurrence)

| Ligne(s) | Nom | Nature | Forme | Tranche |
|---|---|---|---|---|
| 17 | `smichea` | personne | constante/clé | #7662 |

### `tenancy.py` (13 occurrences)

| Ligne(s) | Nom | Nature | Forme | Tranche |
|---|---|---|---|---|
| 49, 375 | `bureau` | rôle | constante/clé | #7634 |
| 49, 370 | `bureau` | rôle | identifiant | #7634 |
| 301, 308, 311, 358, 374, 376 ×2, 408, 416 | `bureau` | rôle | message | #7634 |

### `train_assembly.py` (2 occurrences)

| Ligne(s) | Nom | Nature | Forme | Tranche |
|---|---|---|---|---|
| 67 | `nexlink` | projet | constante/clé | #7663 |
| 67 | `nexlink` | projet | identifiant | #7663 |

### `train_contracts.py` (28 occurrences)

| Ligne(s) | Nom | Nature | Forme | Tranche |
|---|---|---|---|---|
| 121, 123, 125, 127, 132, 139, 140 ×2, 141 ×2, 142 ×2, 143 ×2, 147, 186, 207, 210, 211, 214, 276, 279 ×2, 281 | `nexlink` | projet | identifiant | #7663 |
| 166, 181, 193 | `nexlink` | projet | constante/clé | #7663 |
| 213 | `nexlink` | projet | message | #7663 |

### `train_requalify_review.py` (2 occurrences)

| Ligne(s) | Nom | Nature | Forme | Tranche |
|---|---|---|---|---|
| 21 | `bureau` | rôle | constante/clé | #7634 |
| 34 | `bureau` | rôle | message | #7634 |

### `undeliverable.py` (1 occurrence)

| Ligne(s) | Nom | Nature | Forme | Tranche |
|---|---|---|---|---|
| 135 | `orchestrateur` | rôle | constante/clé | #7661 |

### `unrecorded.py` (1 occurrence)

| Ligne(s) | Nom | Nature | Forme | Tranche |
|---|---|---|---|---|
| 208 | `orchestrateur` | rôle | message | #7661 |

### `work.py` (6 occurrences)

| Ligne(s) | Nom | Nature | Forme | Tranche |
|---|---|---|---|---|
| 105 | `orchestrateur` | rôle | constante/clé | #7661 |
| 503 ×2 | `auditeur` | rôle | message | #7661 |
| 5370 | `bureau` | rôle | message | #7634 |
| 5448 | `nexlink` | projet | import | #7663 |
| 5449 | `nexlink` | projet | identifiant | #7663 |

## 5. Écarts et coordination

1. **#7634 est déjà validé sur `52c5162db` et ne touche pas les migrations.** Son compte
   `bureau` est de 132 occurrences dans le code Python, plus 3 en SQL (migrations 0583, 0584,
   rollback 0584) qu'il laisse. Si #7634 fusionne avant #7660, la liste `#7634` doit passer de
   135 à 3 et les lignes du présent document être régénérées ; sinon
   `test_exception_list_only_shrinks` échouera sur les entrées devenues obsolètes. Inversement,
   si #7660 fusionne d'abord, #7634 doit mettre à jour
   `tests/test_7660_coeur_sans_noms_manaty.py` — fichier absent de son empreinte actuelle.
2. **#7661 ne couvre que cinq fichiers** (`alert_briefs`, `role_timers`, `slo`, `notify`,
   `sous_utilisation`) : 34 occurrences de rôle sont hors de cette empreinte (runner.py 6,
   backlog.py 4, work.py 3, auditor_pass.py 2, context_profile.py 2, file_decoupage.py 2,
   handoff.py 2, hostcheck.py 2, staging_executor.py 2, canon.py 1, debit.py 1, deep_analysis.py
   1, exploitation.py 1, platform/child_environment.py 1, release_cutoff.py 1,
   review_designation_activation.py 1, undeliverable.py 1, unrecorded.py 1). Elles resteront
   dans la liste après la tranche ; le pilote doit soit étendre l'empreinte, soit ouvrir un lot.
3. **#7662 ne couvre pas** migrations/0364_key_holders.sql 2, process_observations_cli.py 1,
   release.py 1, storage/postgres/authority.py 1 : l'hôte `bureau-goal` (aide CLI), l'hôte de
   `release.py`, la personne `smichea` de `storage/postgres/authority.py` et la migration 0364.
   À rattacher ou à ouvrir.
4. **#7663 ne couvre que cinq fichiers** : 129 occurrences `nexlink` sont hors de son empreinte,
   surtout `notify.py` (78), `train_contracts.py` (28), `cli.py` (9). Le canal d'alertes
   `nexlink` de `notify.py` relève de la même décision que la release (spec §5 : « release et
   alertes Nexlink »), mais le corps de #7663 ne cite que la release. Décider : étendre #7663 ou
   ouvrir un lot « alertes Nexlink ».
   **Tranche 4 faite (#7663)** : le moteur `nexlink_release.py` (51 occurrences) est passé dans
   `src/ameesh/modules/nexlink/`, hors du scan du cœur ; `release.py` (3 restants),
   `release_cutoff.py` (0) et `work.py` (0) ne l'importent plus. La dette `#7663` du test descend
   de 228 à 134 ; les 129 occurrences hors empreinte restent, surtout le canal d'alertes de
   `notify.py` (78), à ouvrir en lot séparé.
   **Tranche 3b faite (#7793)** : le canal d'alertes de `notify.py` (78 occurrences) est porté
   par `src/ameesh/modules/nexlink/notify.py`, avec l'enveloppe et le transport signé dans
   `src/ameesh/modules/nexlink/alerts.py` (ex-`src/ameesh/nexlink_alerts.py`) ; le cœur lit le
   canal par `registry.notify_channel(kind)`. `notify.py` ne porte plus aucun nom du lexique ; la
   dette `#7663` du test descend de 134 à 55 (dont `cli.py` 8, le reste hors empreinte). Le
   tableau par fichier ci-dessus reste l'instantané initial de l'inventaire, comme pour #7663.
   **Tranche 3c faite (#7794)** : le contrat CI dynamique du train Nexlink (`train_contracts.py`,
   28 occurrences) est porté par `src/ameesh/modules/nexlink/packet_ci.py` ; le cœur le lit par
   `registry.train_contract(name)` (`train_assembly.contracts()`, 2 occurrences retirées) et le
   doctor (`cli.py`, 8) nomme le canal de module sans nom de projet. La dette `#7663` du test
   descend de 55 à 17.
5. **`manaty` n'a aucune tranche** : 9 occurrences dans 4 fichiers (migrations.py 3,
   alert_briefs.py 2, incident.py 2, packet_guard.py 2). `manaty/ameesh` est le dépôt du cœur,
   `manaty/home` le canon, `manaty/nexlink` un dépôt de projet. Recommandation : les traiter
   avec les projets lus au canon, pas dans la tranche des hôtes.
6. **La garde est stricte dans les deux sens** : elle refuse un nouveau nom et une exception
   devenue inutile. Les tranches qui retirent du code doivent donc inclure
   `tests/test_7660_coeur_sans_noms_manaty.py` dans leur empreinte et baisser les compteurs. La
   liste `EXCEPTIONS` ne peut que décroître ; `RECORDED_BY_TRANCHE` est le plafond historique.
7. **Lignes et rebase** : les lignes sont celles de `main` `aeca7e2eb`. Un rebase peut les
   décaler ; le test compte par fichier et nom et n'en dépend pas. La liste des fichiers peut
   changer avec une fusion voisine : régénérer l'inventaire depuis `occurrences()`.

## 6. Limites

- La garde est lexicale. Elle attrape un nom ajouté, pas une route qui contournerait le canon
  sans écrire le nom.
- Elle ne lit ni les fichiers générés, ni les migrations binaires, ni les notes. Un nom peut
  entrer par un `.json` ou un `.css` sans être vu ; ces fichiers ne portent aujourd'hui aucun
  nom du lexique.
- Les migrations appliquées sont de l'histoire : leurs occurrences SQL ne peuvent pas toutes
  disparaître par édition. La liste les porte comme dette figée, pas comme un oubli.
- Le choix du lexique est une décision de revue : `orchestrateur` et `bureau` sont retenus comme
  identités ; `pilote`, `relecteur`, `composeur` et `veille` restent du vocabulaire générique.
  Si le pilote veut les inclure, le lexique et les compteurs changent ensemble.

## 7. Acceptation

- **Inventaire exhaustif** : 440 occurrences dans 56 fichiers, avec la méthode ci-dessus ; la
  même fonction produit la garde.
- **Test vert sur `main`** : `tests/test_7660_coeur_sans_noms_manaty.py`, 5 tests, aucun
  changement du cœur.
- **Aucun changement de comportement** : seuls ce document et le test sont ajoutés.
