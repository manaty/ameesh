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
        [--profile <profil>] [--base <sha>] [--repo <dépôt>]
        --canon-commit <sha> [--canon-repo <dépôt>]
        [--project <nom>] [--published-ref <ref>] [--json]
```

- sans canon relu (`--canon-commit` absent ou bloc absent) : refus
  `block-missing`, aucune exécution ;
- `--profile` nomme le profil d'offload du guest L159b (dépendances et base du
  projet) ; absent, le profil par défaut de l'hôte s'applique. Le profil de
  projet pose `AMEESH_TEST_VENV` (contrat L159b, `scripts/test.sh`), et le
  script distant lance les parts avec `$AMEESH_TEST_VENV/bin/python3` : sur le
  guest, le python système n'a ni psycopg ni la base jetable, donc un run sans
  profil de projet est rouge/incomplet, jamais un faux vert ;
- contrôles avant tout run : hôte déclaré au canon (sinon `no-vm`) ; chaque
  fichier de contrat déclaré est **relu au SHA candidat**, depuis les objets
  Git (un fichier absent de ce SHA ou illisible refuse), et son
  contenu lie la sélection : tout module qu'il nomme doit être exécuté, et le
  plan ne peut pas compter comme contrat un module absent de ces fichiers ;
  la sélection doit être exactement `classes du canon ∪ contrats` (tout module
  en trop est un élargissement, tout module manquant un contrat perdu) ;
- **le guest n'est pas le poste** (#4071) : le profil d'offload `ameesh`
  déclare dans `GUEST_EXCLUSIONS`, module par module et avec une raison, les
  tests qu'il ne peut pas exécuter (unités systemd, HOME/UID ou canaux Unix
  courts du poste, recette libpq, snapshot sans histoire). `validate` les
  retire du **plan envoyé au guest** — jamais de la suite complète, qui les
  garde — et le rapport les nomme (`excluded`) en séparant leurs échecs
  éventuels (`excluded_failed`) d'un rouge réel : un échec de test exclu est
  non concluant (§3). Aucun rapprochement par motif de nom ; une entrée se
  justifie par un run réel (#4044) ;
- **liaison tête / arbre / plan** : `--repo` (ou le cwd) est l'arbre unique du
  geste. Son `HEAD` doit être le `--sha` annoncé — sinon `head-moved`. Le
  contenu envoyé n'est **pas** une photographie de l'arbre de travail : c'est
  l'arbre **construit** du SHA candidat plus le plan **relu**, figé par son
  identité (`offload.snapshot_with`) et passé à l'offload (`--snapshot`). Un
  fichier non suivi, un flag `assume-unchanged`/`skip-worktree` ou une
  modification locale ne peuvent donc pas entrer dans la preuve (défaut trouvé
  par cx1 : `git status` les masquait alors que la photographie les
  embarquait). Le plan est un chemin **relatif** à cet arbre, refusé s'il en
  sort (`selection-unsafe`), s'il est ignoré par git, s'il remplacerait un
  chemin du SHA candidat, même retiré de l'index local, ou si un parent de son
  chemin y est un fichier (`plan-path-conflict`, conflit D/F : `update-index`
  refuserait l'arbre construit, #1820) ; c'est le texte lu qui est embarqué,
  pas une relecture du disque au moment du run ;
- délai du run : `timeout_seconds` du canon ; exécution par
  `ameesh offload run --fallback none` uniquement. Sur la VM, ce chemin est
  celui du guest L159b (jetons de concurrence `flock`, scope cgroup plafonné par
  l'offre `policy.lend` de l'hôte) : aucune suite lourde n'est lancée hors de
  lui, et il ne contourne pas `ameesh heavy`, qui reste l'outil des agents
  natifs de l'hôte ;
- après le run, la tête locale est relue (l'arbre a-t-il bougé pendant
  l'exécution ?) ; si `--published-ref` est donnée, la référence est relue en
  plus **au distant** (`git ls-remote`). Un mouvement, ou une relecture
  impossible, invalide le rapport (`head-moved`) ;
- le rapport `ameesh-prevalidation/1` est imprimé (`--json`) ; code de sortie 0
  si vert, 1 sinon. **Aucun jalon, aucun verdict** : l'accroche automatique du
  gel n'existe pas encore. Une preuve de gel nomme `--published-ref` : sans
  elle, la tête n'est relue qu'en local, et le rapport le dit (`tree`).

## La preuve est exhaustive

Le rapport ne recopie pas le plan : `modules` est la liste des modules
réellement vus dans le flux `ameesh-test-event/1` (événements `test`), `planned`
celle du plan, `missing` ceux qui manquent, `planned_parts` les parts attendues
et `parts` celles qui ont parlé. Une part sans fin verte, un module attendu muet
ou un flux sans test donnent `run-incomplete` ; une part rouge donne `run-red`.
Seul `fin.statut = succes` est vert : `echec` est rouge, tout autre statut
(`interrompu`, `unknown`, vide) est incomplet, et le `detail` nomme le statut
observé. Un `fin=succes` isolé, sans événement de test, n'est jamais un vert et
ne remplit jamais `modules` avec le plan. Un run arrêté par la porte de l'hôte
(drain : note `stopped on … host stopped|draining`) donne `gate-preempted`,
jamais `no-vm` : la VM existe, c'est l'hôte qui a préempté le run (#2298).

`tree` recopie ce qui a été vérifié et ce qui est parti : `head` observé,
`snapshot` (l'identité exacte du commit envoyé, vérifiable localement par
`git ls-tree -r <snapshot>`), `plan` (le chemin du plan dans cet arbre), et
`published_ref` quand la référence publiée a été relue.

## Rapport persisté (0450)

Le rapport est enregistré dans `prevalidation_reports` (migration 0450), clé
`(work_item_id, head_sha)` : une tête candidate a un seul rapport courant,
réécrire la même clé remplace le précédent. `outcome`, `ok` et `causes` sont
recopiés du rapport complet pour la lecture courte de l'accroche ; `report`
garde le rapport entier (modules, parts, tests, hôte, durée).

L'écriture a lieu **après** le run distant, en une instruction hors de toute
transaction : aucun verrou de base n'est tenu pendant que la VM exécute la
sélection. Un refus avant run (`block-missing`, `no-vm`,
`selection-unsafe`, `plan-path-conflict`) n'écrit rien ; un run rouge ou
incomplet est un résultat et s'enregistre (`ok` faux). Le rapport n'est pas un
verdict : il ne remplace ni #1617 (désignation au gel) ni #1756 (gel dérivé),
et son écriture n'active aucune règle.

Le rapport est **imprimé avant l'écriture** : tant que 0450 n'est pas déployée
(cas de la preuve d'avant fusion), l'écriture échoue en `SchemaMissing`,
l'avertissement nomme la cause et `ameesh migrate`, et le sort du run ne change
pas — un run réel ne se perd jamais parce que la table manque.

Tant que le bloc canon n'est pas publié, la commande refuse donc toujours :
c'est l'effet voulu de l'ordre de mise en service.

## Raccord générique par projet

Le geste est générique : `--project` choisit la fiche du canon, `--canon-repo`
et `--repo` les dépôts, `--plan` le plan du projet, `--profile` le profil
d'offload du guest (dépendances et base du projet, contrat L159b
`AMEESH_TEST_VENV`) ; classes, contrats, hôtes, délai et lanceur de part ne
viennent que du bloc `prevalidation` du canon du projet, jamais d'une valeur
codée en dur.

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

## Accroches automatiques (#2644)

La commande explicite ne suffisait pas : le paquet h-0500 a été regelé trois
fois pour des rouges que la sélection P0/P1 aurait vus avant la suite
complète. `src/ameesh/prevalidation_calls.py` porte les trois appels, avec les
paramètres du bloc `prevalidation` du canon (classes, contrats, hôtes, délai,
lanceur de part) :

- `ci.cmd_ci_run` : pour une tête `integration/**`, la prévalidation passe
  **avant** l'étiquette `ci-full-<sha>`. **Seul un rouge** refuse nommément,
  avec les tests en échec ; aucune étiquette n'est posée. Une prévalidation
  indisponible (carte absente ou de plus de 48 h, VM absente, canon sans bloc,
  sélection non prouvée, run incomplet) ne bloque jamais : le motif est
  journalisé et la demande part (décision pilote du 19/10), sinon tous les
  paquets s'arrêteraient. Une exception `incident|unblock` déjà déclarée
  (`packet_guard`) ne bloque pas non plus.
- `work milestone <lot> frozen` : le rapport est attaché à la tête gelée
  (`prevalidation_reports`), après l'écriture du jalon et la désignation. Un
  rouge ou une indisponibilité est journalisé, jamais bloquant : le gel est
  déjà écrit.
- le passage du train (#2446) : la prévalidation passe avant
  `train_runtime.Runtime.publish`, donc avant le bail et tout push. Un rouge
  arrête la publication ; une indisponibilité est journalisée et la CI
  complète reste la preuve finale.

Le plan L161f est calculé dans un arbre **détaché du commit candidat**, par le
`scripts/test-plan.py` de ce commit. La carte mesurée vient, dans l'ordre,
d'un `--prevalidation-map` explicite, de `carte/carte.json.gz` dans l'arbre,
ou du dernier artefact `carte` vert de GitHub. Sa fraîcheur et l'ascendance de
son commit restent vérifiées par le plan : aucune carte n'est supposée bonne
parce qu'elle a été téléchargée. Sans plan sûr, `prevalidation.validate`
refuse (`selection-full`, `selection-unsafe`) : `ci run` continue et journalise
l'indisponibilité (seul un rouge arrête la demande) ; le gel et le train
continuent et le disent.

Le bloc global vaut pour tous les projets, mais `scripts/test-plan.py` et le
lanceur de part sont des outils du dépôt ameesh (#2794) : avant tout arbre,
carte ou hôte, leur présence est vérifiée au SHA candidat ; un dépôt qui ne
les porte pas (nexlink) est indisponible, cause `tooling-missing`, au lieu
d'un `can't open file` journalisé en `selection-unsafe`. Le journal du gel
n'écrit qu'une note par lot et par cause (#4758) : la clé stable ne cite ni la
tête ni le chemin du dépôt, sans quoi le SHA ferait revenir la note à chaque
gel, `once=True` ne dédupliquant que le texte exact.

## Raccords

- #1616 (lecteur) : extension additive, tête gelée non élargie ; ce delta est
  empilé dessus.
- #1617 (désignation au gel) : ne désigne que sur un gel validé.
- #1756 (gel dérivé) : ne dérive jamais un gel d'un rapport de prévalidation.
- `retires.tsv` (lot, tête) reste tenu par bureau, non dupliqué ici.
