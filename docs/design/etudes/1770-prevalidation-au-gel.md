# #1770 — prévalidation au gel, bloc canon `prevalidation`

Sous-lot de #1606, incident bureau #63617 (paquet PR1293 : têtes rouges hors des
ciblés déclarés par l'auteur, ~1 h perdue). Le gel ne vérifie aujourd'hui que
les ciblés auteur ; la sélection par impact existe côté CI, pas au gel.

## Règle préparée, non activée

Le bloc `prevalidation` du canon autorise une prévalidation automatique du gel :
sélection par impact (carte L161e + plan L161f), exécution sur VM agents par
`ameesh offload run --fallback none`, preuve persistée. Le rapport n'est pas un
verdict et ne remplace ni #1617 (désignation au gel) ni #1756 (gel dérivé).

Ordre de mise en service, impératif :

1. le **lecteur compatible** (ce delta, `src/ameesh/mechanics.py`) est fusionné ;
2. bureau écrit le **bloc canon** (`processus/mecanique.yaml`, schéma `/2`) ;
3. seulement alors l'accroche automatique du gel est activée.

Tant que le bloc n'existe pas, l'accroche automatique reste **inactive** et la
commande explicite refuse, cause nommée `block-missing`.

## Contrat du bloc (schéma `ameesh-mechanics/2`)

Le document sans bloc reste `ameesh-mechanics/1` et compile à l'identique
(témoin `tests/test_1770_mechanics_prevalidation.py`). Le bloc n'est accepté que
sous le schéma `/2` ; un `/2` sans bloc refuse, un `/1` avec bloc refuse.

```yaml
schema: ameesh-mechanics/2
# … blocs existants inchangés …
prevalidation:
  classes: [P0, P1]                       # classes du plan L161f retenues
  contracts: [tests/parts/toujours.txt]   # contrats toujours inclus (chemins dépôt)
  hosts: [ameesh-agents-1]                # hôtes d'exécution autorisés (VM agents)
  timeout_seconds: 3600                   # délai du run distant
  parts_script: scripts/test-parts.py     # lanceur de part du dépôt (facultatif)
```

Chaque clé est relue : clé inconnue, classe inconnue ou en double, chemin non
canonique (absolu, `..` sortant, antislash), hôte hors étiquette ou en double,
délai non entier positif — tout refuse au chargement. `parts_script` est
facultatif : déclaré, il doit porter un chemin de dépôt (`null` refuse) ;
absent, c'est le consommateur qui applique la convention du plan L161f
(`scripts/test-parts.py`, contrat CLI `run --plan --part --driver --flow`),
demandée par le pilote Nexlink pour que son propre outil de sélection se
déclare au bloc sans imposer le nom ameesh.

Le lecteur ne porte aucune valeur par défaut : `Document.prevalidation` et
`Mechanics.prevalidation` valent `None` quand le bloc est absent, et `to_dict()`
n'émet alors pas la clé ; `parts_script` n'est émis que s'il est déclaré.

## Consommateur

`prevalidation.load_parameters(repo, commit, project)` lit le canon au commit
exact et rend `(document, bloc)` ; sans bloc, il lève
`PrevalidationError(cause="block-missing")`. La sélection et l'exécution
(`src/ameesh/prevalidation.py`) ne reçoivent leurs paramètres que du bloc : ni
classe, ni hôte, ni délai codés en dur ; le lanceur de part vient du bloc quand
il le nomme, sinon de la seule convention du schéma.

## Commande explicite

```
ameesh work prevalidate <lot> --sha <tête> --plan <plan.json> --host <vm>
        [--base <sha>] [--repo <dépôt>] --canon-commit <sha> [--canon-repo <dépôt>]
        [--project <nom>] [--published-ref <ref>] [--json]
```

- sans canon relu (`--canon-commit` absent ou bloc absent) : refus
  `block-missing`, aucune exécution ;
- contrôles avant tout run : hôte déclaré au canon (sinon `no-vm`), contrats
  déclarés présents dans le dépôt, sélection exactement `classes du canon ∪
  contrats` (tout module en trop est un élargissement, tout module manquant un
  contrat perdu) ;
- délai du run : `timeout_seconds` du canon ; exécution par
  `ameesh offload run --fallback none` uniquement ;
- après le run, si `--published-ref` est donnée, la référence est relue **au
  distant** (`git ls-remote`) : une tête qui a bougé invalide le rapport
  (`head-moved`) ;
- le rapport `ameesh-prevalidation/1` est imprimé (`--json`) ; code de sortie 0
  si vert, 1 sinon. **Aucun jalon, aucun verdict** : l'accroche automatique du
  gel n'existe pas encore.

## Rapport persisté (0450)

Le rapport est enregistré dans `prevalidation_reports` (migration 0450), clé
`(work_item_id, head_sha)` : une tête candidate a un seul rapport courant,
réécrire la même clé remplace le précédent. `outcome`, `ok` et `causes` sont
recopiés du rapport complet pour la lecture courte de l'accroche ; `report`
garde le rapport entier (modules, parts, tests, hôte, durée).

L'écriture a lieu **après** le run distant, en une instruction hors de toute
transaction : aucun verrou de base n'est tenu pendant que la VM exécute la
sélection. Un refus avant run (`block-missing`, `no-vm`,
`selection-unsafe`) n'écrit rien ; un run rouge ou incomplet est un résultat et
s'enregistre (`ok` faux). Le rapport n'est pas un verdict : il ne remplace ni
#1617 (désignation au gel) ni #1756 (gel dérivé), et son écriture n'active
aucune règle.

Tant que le bloc canon n'est pas publié, la commande refuse donc toujours :
c'est l'effet voulu de l'ordre de mise en service.

## Raccord générique par projet

Le geste est générique : `--project` choisit la fiche du canon, `--canon-repo`
et `--repo` les dépôts, `--plan` le plan du projet ; classes, contrats, hôtes,
délai et lanceur de part ne viennent que du bloc `prevalidation` du canon du
projet, jamais d'une valeur codée en dur.

Chaque projet fournit quatre pièces :

1. **son plan**, schéma `ameesh-test-plan/1` — les parts portent chacune une
   liste explicite de modules ; une part sans liste serait « le reste de la
   suite » et refuse `selection-unsafe` ;
2. **son lanceur de part**, chemin dépôt, contrat CLI
   `run --plan <plan> --part <n> --driver <d> --flow <fichier>` : ameesh garde
   `scripts/test-parts.py` (convention appliquée quand le bloc ne dit rien), un
   autre projet déclare le sien par `parts_script` ;
3. **son bloc canon** : `classes` (P0-P3), `contracts` (chemins dépôt toujours
   sélectionnés), `hosts` (étiquettes des VM d'exécution), `timeout_seconds`,
   `parts_script` s'il déroge à la convention ;
4. **son stockage de rapport**, s'il persiste : ameesh écrit
   `prevalidation_reports` (0450), clé `(work_item_id, head_sha)`.

### Adaptateur Nexlink (validé par son pilote)

Aucune carte Nexlink n'est écrite ici : le bloc et l'hôte d'exécution se
déclarent dans le canon Nexlink, par son pilote. Réponse du pilote (19:57) :
le chemin du lanceur est bien paramétré par le bloc, Nexlink n'a pas
`scripts/test-parts.py`, et son adaptateur sera un lot Nexlink de son ressort
après la fusion de #1770. Points d'accroche, repris de l'étude L161
(« ameesh first, Nexlink next ») :

- base de comparaison `develop`, jamais `main` ;
- sélection par `jest --findRelatedTests` et `vitest related --run`, test de la
  migration modifiée d'abord (`sql-tests.sh <n>`), carte e2e plus tard (L161h) ;
- pas de CI GitHub de lot ni de suite complète : la prévalidation n'exécute que
  la sélection du plan ;
- un lanceur de part Nexlink déclaré par `parts_script` ;
- son propre stockage de rapport si Nexlink persiste les siens.

L'application à Nexlink appartient à son pilote (§7 : une règle vaut pour tous
les projets, avec un lot d'application par projet) : ce lot ne crée ni carte ni
bloc Nexlink, et ne lance aucune de ses suites.

## Raccords

- #1616 (lecteur) : extension additive, tête gelée non élargie ; ce delta est
  empilé dessus.
- #1617 (désignation au gel) : ne désigne que sur un gel validé.
- #1756 (gel dérivé) : ne dérive jamais un gel d'un rapport de prévalidation.
- `retires.tsv` (lot, tête) reste tenu par bureau, non dupliqué ici.
