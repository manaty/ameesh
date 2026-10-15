# #1465 — preuve exacte d'origine : le relecteur qui corrige et fusionne

- **Règle** : home#81 (owner, 15/10/2026 vers 03:10), lue à `1f66ca2` et diffusée
  à toute l'équipe ameesh — « le relecteur corrige et fusionne dès le premier
  tour » : plus d'aller-retour auteur ↔ relecteur ; le relecteur pousse sa
  correction ; lot léger : fusion auto à la CI verte ; lot sensible : la seconde
  famille relit **avec** la correction ; correction > ~50 lignes ou de
  conception : retour à l'auteur. Elle prolonge le canon §2 (« au-delà de deux
  tours de relecture, le relecteur corrige lui-même »).
- **Lot** : #1465, outillage et consignes. **Dépendance aval** : le train de
  paquets #1467 (`train_review.qualify`) exige, pour chaque commit de la tête,
  l'acteur, la session, le tour et le harnais qui l'ont produit — sans jointure
  temporelle (mesh-design, 15/10 03:53).

## 1. Ce qu'une correction par le relecteur change

Quand le relecteur pousse sa correction, il devient **auteur** des commits
qu'il crée. La couverture d'avis doit alors le compter comme auteur à part
entière : sa famille et sa session ne peuvent pas couvrir ses propres commits,
et chaque autre auteur de la tête garde ses avis indépendants. Il faut donc
savoir, **par commit**, qui l'a produit — pas seulement qui a gelé la branche.

## 2. Sources examinées, et ce qu'elles ne prouvent pas

| Source | Ce qu'elle porte | Pourquoi elle ne prouve pas commit → tour |
| --- | --- | --- |
| SQL0013 `work_item_milestones` | jalon : `id`, `actor`, `at`, `sha`, `verdict` | le `sha` d'un gel est une déclaration du **dernier** état ; un commit antérieur ou une correction du relecteur n'y sont pas |
| SQL0014 `turn_costs` | tour terminé : `agent`, `harness`, `turn`, `session`, `recorded_at` | aucune colonne de commit ; « le tour le plus proche » est une jointure temporelle |
| SQL0066 `persona_sessions` | session : harnais, début, fin | `record_start` peut rouvrir la même ligne : l'intervalle seul ne couvre pas toutes les reprises |
| `review_lot._freeze` / `_turns_harness` / `_author_at_freeze` (#1053, PR #815) | tours et harnais de l'auteur **au gel** | descriptif au gel, pas une preuve par commit ni du tour qui émet un verdict |
| registre (`registry.live_harness`) | harnais de la session vivante | dit la famille d'un agent à un instant, pas l'auteur d'un commit |
| auteur Git du commit (`GIT_AUTHOR_EMAIL`) | nom posé par le runner | falsifiable (`git commit --author`), et un nom n'est pas une identité de tour |

Aucune de ces sources ne relie un commit à un tour **par construction**. La
seule preuve est une écriture faite à l'instant du fait, par l'outil.

## 3. Le contrat livré

Table `provenance_receipts` (migration **0445**, ajout seul) : une ligne par
sujet, `commit:<sha complet>` ou `milestone:<id>`, portant `sha`, `actor`,
`session`, `turn`, `harness`, `work_item_id` (facultatif), `at`.

Fournisseur importable — `src/ameesh/review_provenance.py` :

```python
from ameesh import review_provenance as prov
recu = prov.receipt_for_commit(db, sha)          # dict ou None
recu = prov.receipt_for_milestone(db, jalon_id)  # dict ou None
```

Le reçu porte exactement les champs de `train_review.Receipt` : `subject`,
`commit`, `actor`, `session`, `turn`, `harness`, `record`,
`milestone`. Pour un jalon, `milestone` recopie `id`, `actor`, `at_ts`, `sha`,
`verdict` du fait, et `commit` est le commit complet que le `sha` déclaré
résout (abréviation résolue à la déclaration, dépôt sous la main).

Règles de refus :

- **absence de ligne = origine inconnue** → `None`, jamais un voisin temporel ;
- une preuve sans son fait (ou l'inverse) est inconnue ;
- preuve et fait incohérents (acteur différent, abréviation qui ne résout pas le
  commit enregistré) → `ProvenanceError`, pas de rabotage ;
- la première écriture fait foi : une seconde écriture du même sujet ne réécrit
  rien, et une identité divergente est signalée à l'appelant.

Écriture, à l'instant du fait :

- **jalon** : la même transaction que `work_item_milestones` (le fait et sa
  preuve naissent ensemble) ;
- **commit** : `ameesh provenance record --commit <sha>` appelé par le hook
  `post-commit` du dépôt de l'agent, donc pour chaque commit, dans le tour qui
  le crée. Le tour (`AMEESH_TURN_ID`) et le harnais (`AMEESH_HARNESS`) viennent
  de l'environnement du runner ; la session, du registre vivant de l'acteur
  (`registry.live_harness`, #1037), recontrôlée contre l'étiquette du tour.

## 4. État et suite

Livré dans les points réversibles de `lot/1465-preuves-par-commit` : migration
0445, contrat de stockage, fournisseur (`receipt_for_commit`,
`receipt_for_milestone`), écriture (`ameesh provenance record --commit`,
`ameesh provenance install`, jalon gel/verdict et sa preuve dans la même
transaction que `work_item_milestones`) et témoins
(`tests/test_1465_provenance.py`).

Reste avant gel : poser le hook au démarrage de session (runner), faire relire
les deux familles requises (SQL sensible, canon §2). L'historique **antérieur**
à la migration restera `unknown` : c'est le prix d'une preuve honnête, et le
train #1467 devra qualifier des commits enregistrés.
