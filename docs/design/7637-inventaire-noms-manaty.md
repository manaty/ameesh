# Noms Manaty du cœur — inventaire et garde (#7637)

> **Lot** : #7637 (issue `manaty/ameesh#5828`), inventaire final après les
> tranches #7792 à #7796 et #7813 ; ce document est né avec #7660 (tranche 1).
> **Date** : 19/11/2026. **Base** : `main` `6209a821cfa0188e11e1d8cc3889eb09647f1386`.
> **Portée** : `src/ameesh/`, hors `modules/`, tests et notes.
> **Garde** : `tests/test_7660_coeur_sans_noms_manaty.py`. L'inventaire est
> régénéré depuis `occurrences()` ; `EXCEPTIONS` porte le même contenu agrégé
> par fichier et nom, et `INSTRUCTION_EXCEPTIONS` couvre les consignes.

## 1. Ce qui est compté

Un **nom Manaty** est un nom propre que le cœur ne peut pas connaître d'un mesh à l'autre : un rôle de gouvernance, un hôte, une personne ou un projet. Le canon les porte (spec §5, « noms des rôles, hôtes, personnes, processus, routes d'alerte ») ; le cœur les lit, il ne les écrit pas.

Le lexique est fermé et explicite :

| Famille | Noms (formes reconnues) | Occurrences | Fichiers | Tranche |
|---|---|---|---|---|
| Rôle | `mesh-design`, `auditeur`, `orchestrateur`, `bureau` | 5 | 4 | #7634, #7661 |
| Hôte | `ameesh-agents-1`, `ameesh-agents-2`, `ameesh-agents-3`, `manaty-mesh-1`, `pc-smichea`, `bureau-goal` | 0 | 0 | #7662 |
| Personne | `smichea` | 2 | 1 | #7662 |
| Projet | `nexlink`, `manaty` | 0 | 0 | #7663 |
| **Total** | | **7** | **5** | |

Après les six tranches de code (#7792 à #7796, #7813), il reste **7 occurrences
dans 5 fichiers**, contre 440 dans 56 au premier inventaire (#7660). Plus aucun
`nexlink` ni `manaty` ne figure dans le cœur : cinq occurrences sont figées dans
des migrations déjà appliquées et deux sont les jetons de rôle autorisés par la
décision du 18/11.

Le critère, amendé par mesh-design le 18/11/2026, autorise ces **7 occurrences**
et il est atteint : le cœur ne porte plus aucun nom de rôle, d'hôte, de personne
ou de projet hors des cinq migrations appliquées et des deux jetons de
`roles.py`.

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
- **`modules/`** : un module porte son nom et son vocabulaire. C'est la
  destination des tranches #7663, #7793 et #7794.
- **Tests, docstrings, commentaires, notes, `COMMENT ON` SQL** : le corps du lot exclut les
  notes. Le scan lit le code exécutable, pas les explications.
- **`deploy/`, `scripts/`, `docs/`, `.json`, `.css`, `.md`** : hors du périmètre `src/ameesh/`
  ou non exécutables, sauf les consignes (#7796, ci-dessous).
- **Consignes lues par les agents (`src/ameesh/consignes/*.md`)** : lues en entier par la
  garde (#7796). Leurs noms viennent du canon (variable `$staging_hosts` de `veille.md`,
  lue à la séquence staging des fiches Host) ou de mots génériques ; seule exception
  justifiée : le chemin `docs/ORCHESTRATEUR.md`, nom d'un document du dépôt.

## 2. Méthode de recherche

Le scan est celui de `tests/test_7660_coeur_sans_noms_manaty.py` ; l'inventaire et la garde ne peuvent pas diverger.

1. Fichiers : `src/ameesh/**` de suffixe `.py`, `.sql`, `.html`, `.js`, `.cjs`, sauf
   `src/ameesh/modules/**`.
2. Python : `ast.parse` ; sont lus les littéraux de chaîne hors docstrings,
   les identifiants (`Name`, `Attribute`, `arg`, `keyword`, et depuis #7796
   `Global`, `Nonlocal`), les noms de fonctions/classes et les imports (module
   et alias). Un identifiant est lu `_` → `-` : `bureau_goal` est l'hôte
   `bureau-goal`, jamais le rôle `bureau`. Un littéral multi-ligne
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

La fonction `occurrences()` du test rend les lignes de l'inventaire (7 sur
cette base) ; `EXCEPTIONS` porte le même contenu agrégé en 6 entrées. Les
consignes (`consignes/*.md`) sont lues à part par `instruction_occurrences()` ;
`INSTRUCTION_EXCEPTIONS` porte leur unique exception (§5.4).

## 3. Synthèse

| Statut | Occurrences | Fichiers | Détail |
|---|---|---|---|
| Figées dans des migrations appliquées | 5 | 4 | `bureau` 3, `smichea` 2 |
| Autorisées : vocabulaire des fiches | 2 | 1 | `auditeur`, `orchestrateur` dans `roles.py` (décision 18/11) |
| **Total** | **7** | **5** | |

Un fichier peut porter plusieurs statuts : les colonnes « Fichiers » ne
s'additionnent pas, le total est le nombre de fichiers distincts.

- **#7634** : le nom `bureau` sort du cœur (routage humain par l'avatar) ;
  seules restent 3 occurrences SQL dans les migrations 0583, 0584 et leur
  rollback, figées dans l'histoire du schéma.
- **#7661, #7792** : les rôles `mesh-design`, `auditeur`, `orchestrateur` sont
  lus à la fiche Agent. `roles.py` ne garde que deux jetons par défaut
  (`auditeur`, `orchestrateur`), autorisés par la décision du 18/11.
- **#7662, #7795** : les hôtes et la personne `smichea` sont lus au canon ;
  restent 2 `smichea` dans la migration 0364. Les 9 `manaty` sont retirés.
- **#7663, #7793, #7794, #7795, #7813** : le module Nexlink reprend la release,
  le train et le canal d'alertes. La dette `nexlink` passe de 228 à 0 ; les 13
  dernières occurrences sont retirées par #7813.
- **#7796** : les consignes `chat.md` et `veille.md` sont génériques, sauf le
  chemin `docs/ORCHESTRATEUR.md` de `veille.md` (§5.4), et la garde lit
  `Global`/`Nonlocal`.

La sixième tranche #7813 (décision mesh-design du 18/11/2026) a retiré les
13 derniers `nexlink` ; la cible est atteinte à **7 occurrences dans
5 fichiers** (§5.2, §7).

## 4. Inventaire

Pour chaque fichier : les lignes, le nom, sa nature, sa forme dans le code, la
tranche qui le porte et l'état de l'occurrence. Une même ligne peut apparaître
sur deux lignes de tableau si elle porte deux formes différentes ; `×N`
signale N occurrences du même nom sur la même ligne.

État : `figée (migration)` = SQL d'une migration déjà appliquée, non réécrit ;
`autorisée (décision 18/11)` = jeton de vocabulaire des fiches, autorisé par la
décision mesh-design.

### `migrations/0364_key_holders.sql` (2 occurrences)

| Ligne(s) | Nom | Nature | Forme | Tranche | État |
|---|---|---|---|---|---|
| 381 ×2 | `smichea` | personne | sql | #7662 | figée (migration) |

### `migrations/0583_bureau_staging_catchup.sql` (1 occurrence)

| Ligne(s) | Nom | Nature | Forme | Tranche | État |
|---|---|---|---|---|---|
| 158 | `bureau` | rôle | sql | #7634 | figée (migration) |

### `migrations/0584_bureau_catchup_timestamp.sql` (1 occurrence)

| Ligne(s) | Nom | Nature | Forme | Tranche | État |
|---|---|---|---|---|---|
| 145 | `bureau` | rôle | sql | #7634 | figée (migration) |

### `migrations/rollback/0584_restore.sql` (1 occurrence)

| Ligne(s) | Nom | Nature | Forme | Tranche | État |
|---|---|---|---|---|---|
| 144 | `bureau` | rôle | sql | #7634 | figée (migration) |

### `roles.py` (2 occurrences)

| Ligne(s) | Nom | Nature | Forme | Tranche | État |
|---|---|---|---|---|---|
| 15 | `auditeur` | rôle | constante/clé | #7661 | autorisée (décision 18/11) |
| 17 | `orchestrateur` | rôle | constante/clé | #7661 | autorisée (décision 18/11) |

## 5. Ce qui reste autorisé, et pourquoi

### 5.1 Migrations appliquées (5)

| Fichier:ligne | Nom | Pourquoi |
|---|---|---|
| `migrations/0364_key_holders.sql:381` ×2 | `smichea` | porteur historique d'une clé |
| `migrations/0583_bureau_staging_catchup.sql:158` | `bureau` | rattrapage staging |
| `migrations/0584_bureau_catchup_timestamp.sql:145` | `bureau` | horodatage du rattrapage |
| `migrations/rollback/0584_restore.sql:144` | `bureau` | restauration de la migration 0584 |

Une migration appliquée est de l'histoire : la réécrire ne changerait pas les
bases. Ces cinq occurrences sont la dette figée prévue par #7634 et #7795,
pas un oubli.

### 5.2 Jetons de rôle de `roles.py` (2)

`roles.py:15` et `roles.py:17` portent `AUDITOR_ROLES = ("auditeur",
"auditor")` et `ORCHESTRATOR_ROLES = ("orchestrateur", "orchestrator")`.
#7792 a ramené les exceptions de rôle de 47 à 2 en déplaçant la reconnaissance
sur la déclaration `roles:` des fiches Agent : le cœur ne reconnaît plus un
rôle sur le titre d'une persona. Les deux jetons restants sont le vocabulaire
par défaut de la fiche ; un mesh dont le canon emploie d'autres mots n'est pas
reconnu comme auditeur ou orchestrateur sans passer par ces synonymes.

Décision mesh-design du 18/11/2026 : ces deux jetons sont **autorisés** comme
vocabulaire générique comparé au `roles:` déclaré par la fiche ; ce n'est pas
un nom de persona. Avec les cinq migrations, ce sont les seules occurrences
autorisées.

### 5.3 `nexlink` : retirés par #7813

Les 13 occurrences `nexlink` qui restaient après #7795 ont été retirées par la
sixième tranche #7813 (commits `e28680015`, `4f0017252` et `80e4abf53`,
fusionnés dans `main` `6209a821c`) :

| Fichier | Ce qui a changé |
|---|---|
| `approve/config.py` | zones et hôtes réservés lus au module et à la configuration ; la propriété de la décision 0026 est gardée |
| `ci.py` | message générique et schéma du plan de dispatch déclaré par le module |
| `cost.py` | prix sous `~/.config/ameesh/` |
| `fingerprints.py`, `server_faults.py` | schéma de page générique ; l'ancien nom devient un alias du module Nexlink |
| `host_cli.py` | textes d'interface génériques |
| `release.py` | projet par défaut lu à la configuration, sans déduction |
| `role_timers.py` | canaux de push lus au registre |

La liste autorisée du test #7660 est passée de 20 à 7 occurrences dans le même
lot ; plus aucun `nexlink` ne figure dans le cœur.

### 5.4 Consignes (1)

`consignes/veille.md:5` porte `ORCHESTRATEUR` dans le chemin
`docs/ORCHESTRATEUR.md` : c'est le nom d'un document du dépôt, pas un rôle.
`INSTRUCTION_EXCEPTIONS` l'assume et la garde le vérifie ; aucune autre
consigne ne nomme un rôle, un hôte, une personne ou un projet.

### 5.5 La garde est stricte dans les deux sens

Elle refuse un nouveau nom et une exception devenue inutile. Toute tranche qui
retire du code inclut `tests/test_7660_coeur_sans_noms_manaty.py` dans son
empreinte et baisse les compteurs. La liste `EXCEPTIONS` ne peut que décroître ;
`RECORDED_BY_TRANCHE` est le plafond historique. #7813 a modifié le test dans
le même commit que le retrait des 13 `nexlink` ; `EXCEPTIONS` compte désormais
6 entrées pour 7 occurrences.

### 5.6 Lignes et rebase

Les lignes sont celles de `main` `6209a821c`. Un rebase peut les décaler ; le
test compte par fichier et nom et n'en dépend pas. La liste des fichiers peut
changer avec une fusion voisine : régénérer l'inventaire depuis
`occurrences()`.

## 6. Limites

- La garde est lexicale. Elle attrape un nom ajouté, pas une route qui
  contournerait le canon sans écrire le nom.
- Elle ne lit ni les fichiers générés, ni les migrations binaires, ni les
  notes. Un nom peut entrer par un `.json` ou un `.css` sans être vu ; ces
  fichiers ne portent aujourd'hui aucun nom du lexique.
- Les migrations appliquées sont de l'histoire : leurs occurrences SQL ne
  peuvent pas toutes disparaître par édition. La liste les porte comme dette
  figée, pas comme un oubli.
- Le choix du lexique est une décision de revue : `orchestrateur` et `bureau`
  sont retenus comme identités ; `pilote`, `relecteur`, `composeur` et `veille`
  restent du vocabulaire générique. Les deux jetons de `roles.py` sont autorisés
  par la décision du 18/11 ; ils ne changent que si le lexique change.
- L'inventaire du §4 ne compte que le cœur exécutable ; les consignes sont
  lues à part (§5.4) et n'entrent pas dans le total du §4.

## 7. Acceptation

| Critère du lot | Verdict |
|---|---|
| Inventaire exhaustif | **oui** — 7 occurrences dans 5 fichiers, régénérées depuis `occurrences()` ; les consignes sont traitées à part (§5.4). |
| Garde verte | **oui** — les 7 tests de `tests/test_7660_coeur_sans_noms_manaty.py` sont verts sur la base ; 245 tests ciblés (garde, rôles, hôtes, module Nexlink, train, notifications, approbation, profil nu et tranche #7813) le sont aussi. |
| Plus aucun nom de rôle, d'hôte ou de projet Manaty dans le cœur, hors tests et notes | **oui** — 7 occurrences autorisées (5 migrations + 2 jetons de `roles.py`), 0 hors critère. |
| Le mesh Manaty se comporte à l'identique avec son canon | **couvert par les tranches** — #7792 à #7796 et #7813 ont livré leurs tests ciblés ; ce lot ne change pas le code. |
| Le profil nu démarre sans ces rôles ni ces hôtes | **oui** — 0 hôte et 0 projet dans le cœur ; les 2 jetons sont le vocabulaire par défaut autorisé, et `test_l98_bare_profile` / `test_l104_bare_canon` sont verts. |
| Aucun changement de comportement | **oui** — ce lot ne modifie que ce document. |

L'inventaire est à jour sur `main` `6209a821c`, la garde est verte et le
critère amendé est atteint à **7 occurrences dans 5 fichiers**. La tête est
gelée pour la revue ; le lot part au paquet d'intégration.
