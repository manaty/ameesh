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

Écriture, à l'instant du fait, et **bornée** (frontières fixées après les
témoins isolés de cx3, 15/10 04:27–04:29 puis 04:37) :

- **jalon** : la même transaction que `work_item_milestones` (le fait et sa
  preuve naissent ensemble) ;
- **commit** : la paire de hooks du dépôt de l'agent. Le `pre-commit` écrit une
  **attestation** — identité du tour, HEAD et arbre de départ, aléa — dans le
  répertoire git du worktree (`--absolute-git-dir` : les hooks sont partagés,
  l'attestation ne l'est pas). Le `post-commit` écrit le **fait de création**
  de l'objet né (`sha`, arbre, parents), repris de la dernière entrée de reflog
  de HEAD — qui n'est un `git commit` que si l'objet vient de naître — puis
  appelle `ameesh provenance record --commit HEAD`, qui exige l'attestation ET
  le fait de création, liés par le même aléa, vérifie que le commit en est bien
  la création (il part du HEAD attesté, fusion en cours résolue comprise, ou en
  est la réécriture — amend : mêmes parents que le HEAD attesté —, avec l'arbre
  attesté), les **consomme** et écrit la preuve. Trois bornes : HEAD du dépôt
  sous la main, committer de l'acteur du tour (`<acteur>@ameesh.invalid`, posé
  par le runner), fait de création de **ce** tour. Un objet hérité ou seulement
  restauré par `reset`/`checkout`, même committé par le même agent, n'a pas de
  fait de création : son origine reste inconnue (écart cx3, 15/10 06:37 ; B1
  cx2/Codex, 15/10 12:12), jamais réattribuée au nouveau tour par le seul
  couple HEAD + committer ni par la forme attestée ;
- **chemins couverts** : `git commit` (y compris `-a`, `--amend`, et la
  résolution d'une fusion par `git commit`) naît avec son attestation et son
  fait de création. Les commits créés sans `pre-commit` (`git merge --no-ff`
  sans conflit, `git rebase`, `git cherry-pick`) restent `unknown` : le
  fournisseur ne devine jamais, et un écrivain `post-merge`/`post-rewrite`
  sera ajouté si le train #1467 a besoin de ces chemins ;
- le tour (`AMEESH_TURN_ID`) et le harnais (`AMEESH_HARNESS`) viennent de
  l'environnement du runner ; la session, du registre vivant de l'acteur
  (`registry.live_harness`, #1037), recontrôlée contre l'étiquette du tour ;
- un tour ne signe que pour **son** agent : `capture` refuse un acteur qui
  n'est pas `AGENT_MAIL_NAME`, et un jalon déclaré pour un autre acteur
  s'écrit sans preuve (origine inconnue).

## 4. État et suite

Livré dans les points réversibles de `lot/1465-preuves-par-commit` : migration
0445, contrat de stockage, fournisseur (`receipt_for_commit`,
`receipt_for_milestone`), écriture (`ameesh provenance record --commit`,
`ameesh provenance intent`, `ameesh provenance created`,
`ameesh provenance install` — paire de hooks —, jalon gel/verdict et sa preuve
dans la même transaction que `work_item_milestones`), pose des hooks à
l'ouverture de session par le runner (jamais en mode relayé) et témoins
(`tests/test_1465_provenance.py`).

Reste avant gel : faire relire
les deux familles requises (SQL sensible, canon §2). L'historique **antérieur**
à la migration restera `unknown` : c'est le prix d'une preuve honnête, et le
train #1467 devra qualifier des commits enregistrés.

## 5. Durcissement après la relecture Claude (lot #1534)

#1465 fusionné, les N de la relecture Claude sont traités sans rouvrir la tête
gelée :

- un jalon dont le `sha` déclaré n'est pas une abréviation hexadécimale
  (`HEAD`, nom de branche) n'écrit plus de preuve — le fait garde son sha, et
  `receipt_for_milestone` n'a plus de couple preuve/fait incohérent à refuser
  pour toujours ;
- les valeurs passées à `git rev-parse` sont bornées par `--end-of-options` :
  une valeur qui commence par `-` reste une révision, jamais une option ;
- les hooks suivent `core.hooksPath` (`git rev-parse --git-path hooks`) : ils
  sont posés là où Git les exécute, pas dans `<git-common-dir>/hooks` ;
- un hook étranger n'est plus reconnu sur la seule mention `ameesh #1465` mais
  sur la ligne de signature exacte de chaque hook (ou un contenu identique) ;
- `ameesh provenance install` nomme la paire de hooks qu'il pose ;
- le gabarit de relecture couvre la correction d'un B par le relecteur, le
  retour à l'auteur au-delà de ~50 lignes ou pour un changement de conception,
  la seconde famille qui relit avec la correction et la preuve d'origine des
  commits du correcteur.

La route du défaut non bloquant vers le backlog (N5) était déjà alignée par
#1391 avant ce lot.

## 6. Repli de la désignation au gel : le journal des tours (#2721, 19/10)

Un rebase ou un amend crée un **nouvel objet** : le reçu de #1465 reste sur
l'ancien, et le gel lit le nouveau. Un commit antérieur aux hooks n'en a jamais
eu. La désignation au gel restait donc en attente « author provenance unknown »
alors que le commit avait bien été produit par un agent connu (#652, #640,
#2691/#2693).

#2681 a ramené la lecture des auteurs à une seule source partagée par le train,
la porte et la désignation (`review_authors`). Pour un commit sans reçu, cette
lecture prend le **committer** — l'agent qui a créé l'objet, rebase ou amend
compris — et sa famille au journal des tours (`turn_costs`) **à l'instant du
commit**, pas le harnais déclaré au registre : un harnais emprunté reste le
harnais qui a réellement tourné. Un committer distinct de l'auteur de référence
doit être un agent du registre ; un tiers sans reçu reste inconnu. Pour un
relecteur désigné, un commit sans reçu reste **sa** correction, avec sa famille
de session : le seuil du §2 ne décide que de la recevabilité process (retour à
l'auteur), jamais de l'indépendance (#2888).

Le repli est une **trace**, pas un reçu : la marque de désignation porte
`proof: turn-journal` et `fallback_commits` (les SHA admis par ce chemin), et
`receipt_for_commit` continue de rendre `None` — le train ne lit que les preuves
exactes. Témoins : un vrai rebase d'un commit muni d'un reçu gèle le nouvel
objet et désigne les deux familles indépendantes
(`test_a_rebased_commit_is_read_from_the_turn_journal`), un amend substantiel
par un relecteur le compte comme auteur
(`test_an_inter_agent_amend_counts_the_committer_as_author`), et un harnais
emprunté est lu au tour, pas au registre
(`test_a_borrowed_harness_uses_the_commit_turn_family`).

## 7. Réécriture d'historique : les auteurs des gels antérieurs (#2924, 21/10)

Une consolidation (squash, commit unique sur une cible avancée) crée un objet
neuf : les reçus restent sur les anciens commits et le nouveau ne les porte
pas. Une réécriture **à contenu identique** effaçait donc la provenance des
correcteurs — cas réel #286, `40192bd5ff4a`, arbre identique à `1f827402b`
fusionné avec main — et leurs familles redevenaient « indépendantes ».

`review_authors.reading` relit les gels antérieurs du même lot et reprend leurs
auteurs tant que leur contenu subsiste :

- gel ancêtre de la tête et absent de la base : son contenu est dans l'histoire ;
- arbre de la tête égal à celui du gel, ou à la fusion du gel avec la base ;
- sinon un commit n'est tenu pour réécrit que si **aucune** de ses lignes
  significatives ne subsiste : une ligne ou un commentaire modifié ne suffit
  jamais à retirer l'auteur (choix prudent, canon §2).

Un correctif réellement réécrit n'est pas repris (cas admissible, décision
documentée) ; une réécriture partielle reste attribuée et une lecture illisible
ou ambiguë conserve l'auteur. Un journal de jalons illisible ne rend jamais une
liste vide : la lecture stricte refuse, la désignation attend (unknown).

Un acteur peut avoir écrit sous **plusieurs familles** dans un gel antérieur
(même agent historiquement Codex puis DeepSeek, squash signé DeepSeek). Le
lecteur transporte tout l'ensemble prouvé, pas la seule réduction ambiguë à
une chaîne vide : `families_out` garde les deux familles, `authors` rend une
famille vide (ambiguë), la désignation attend, la porte refuse de deviner et
le train refuse une famille inconnue. Une preuve courante ne doit jamais
effacer une famille historique.

Un gel antérieur absent du dépôt de lecture — le clone scratch de `merge-pr`
ne récupère que la cible et `refs/pull/N/head` — n'est plus un refus
systématique : la porte tente de le rapatrier **par SHA** depuis le dépôt de
l'agent puis l'origine du projet, sans déplacer aucune ref, et ne refuse que si
la récupération échoue (le refus fermé de B5 reste la règle quand les objets
manquent réellement). Le cas #286 (sept gels non ancêtres de la tête
`a8da5f82`, lus depuis un dépôt qui ne porte que la cible et la tête) est
ainsi lisible sans `ProvenanceError`. Consigner au gel la lecture de ses
auteurs reste une suite utile (elle éviterait le réseau), mais n'est plus
nécessaire pour qu'un lot regelé après rebase passe la lecture stricte.

La même lecture sert la désignation, la porte et le train (`base`/`head`
passés par chaque appelant) ; les correcteurs antérieurs restent tracés pour
l'exclusion personnelle. Un lot porteur de paquet reste hors de la reprise
(#2803 : seule son delta propre est relu). Témoins :
`tests/test_2924_history_rewrite_provenance.py` (squash à arbre identique,
consolidation sur cible avancée, réécriture réelle, changement sans rapport,
ensembles de familles ambigus, gel absent récupéré par SHA — avec et sans
correcteur — et absence non récupérable refusée).
