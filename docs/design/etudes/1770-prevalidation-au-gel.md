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
```

Chaque clé est obligatoire et relue : clé inconnue, classe inconnue ou en
double, chemin non canonique (absolu, `..` sortant, antislash), hôte hors
étiquette ou en double, délai non entier positif — tout refuse au chargement.

Le lecteur ne porte aucune valeur par défaut : `Document.prevalidation` et
`Mechanics.prevalidation` valent `None` quand le bloc est absent, et `to_dict()`
n'émet alors pas la clé.

## Consommateur

`prevalidation.load_parameters(repo, commit, project)` lit le canon au commit
exact et rend `(document, bloc)` ; sans bloc, il lève
`PrevalidationError(cause="block-missing")`. La sélection et l'exécution
(`src/ameesh/prevalidation.py`) ne reçoivent leurs paramètres que du bloc : ni
classe, ni hôte, ni délai codés en dur.

## Raccords

- #1616 (lecteur) : extension additive, tête gelée non élargie ; ce delta est
  empilé dessus.
- #1617 (désignation au gel) : ne désigne que sur un gel validé.
- #1756 (gel dérivé) : ne dérive jamais un gel d'un rapport de prévalidation.
- `retires.tsv` (lot, tête) reste tenu par bureau, non dupliqué ici.
