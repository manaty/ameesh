# Port factuel des diagnostics de paquet

Source : qualification mesh-design du 15 octobre 2026 à 16:09, portée du
mandat #1606(1) dans #1467. #1616 fournit la règle de processus ; #1681
consomme le diagnostic après atterrissage. Le même contrat factuel sert aux
deux consommateurs, sans second moteur ni nouvelle CI diagnostique.

`train_diagnosis.unresolved(plan, ci)` rend `ameesh-train-diagnosis/1` pour
une CI complète rouge sur le commit et l'arbre exacts. Runtime conserve ce
rapport dans la preuve CI ; Engine le conserve dans son état avant de
signaler le rouge. En construction/bêta, aucun lot n'est accusé ni retiré, aucune CI n'est relancée
et aucun vert n'est déduit. Les deux résultats sont actuellement `unknown`.

Le sujet du rapport porte `source`, `target`, `base`, `head`, `tree`, puis
chaque workflow, run, tentative et job rouge observé. Un producteur factuel
qualifié doit compléter les données suivantes avant toute autre décision :

| Donnée | Liaison et preuve requises |
| --- | --- |
| Échecs de tests | Liste complète des tests en échec, avec workflow, run, tentative, job, module, identifiant du test et commande réellement exécutée ; aucun agrégat ne remplace cette liste. |
| Impact | Source versionnée de la carte, commit couvert, complétude du périmètre et liens test→code/données/tables ; les noms de fichiers semblables ne prouvent aucun lien. |
| Membres | `work_item_id`, génération, gel et `frozen_sha` du manifeste ; diff du membre depuis sa base de divergence, pour éviter d'attribuer le delta de la cible à ce lot. |
| Vert local | Reçu d'exécution produit par une source authentifiée, sur le même commit, arbre, test et commande que le rouge, avec environnement, résultat et provenance de l'exécution. Une note ou un champ libre n'est pas un reçu. |
| Culpabilité | Preuve causale explicite sur les lots et prérequis concernés. Un candidat unique dans une carte d'impact reste un candidat ; plusieurs candidats ne désignent aucun auteur fautif. |

Sources déjà présentes : `train_ci.GitHub.read` fournit les runs/tentatives
et jobs exacts ; `storage.ci.failures(repo, run_id)` fournit le flux des tests,
mais son seul identifiant de run ne prouve pas la tentative ; `scripts/map.py`
décrit la carte mesurée ameesh (`ameesh-carte/1`, blocs, données, dossiers,
tables et commit). Aucune carte Nexlink complète ni reçu local de paquet
n'est sélectionné ici. Leur absence reste indiquée dans `missing`.

Le commit final du train est multiparent : premier parent `plan.base`, puis
les têtes `frozen_sha` ordonnées. Il porte un trailer par lot, mais ses
merges intermédiaires ne sont pas publiés dans son ascendance. Le lecteur
après atterrissage doit utiliser le manifeste dont `plan.head` correspond
au commit observé et dont `source` correspond au dépôt. La clé de lecture
est `land.lock_key(project, target)` ; aucun consommateur ne reconstruit un
alias de cible. L'auteur vient de la provenance historique, pas du committer
du train. Les preuves membres sont sous `plan.proof.members`.

Quand ces preuves deviennent disponibles, le consommateur applique les
champs compilés `ci.environment_red`, `ci.faulty_lot` et
`merge.post_merge_red` du même canon exact. Le rapport courant `unknown`
n'autorise aucune exemption environnementale, exclusion ou attribution.

## Lecture après atterrissage

`train_incidents.PacketFacts(db, project, path, impact=..., fallback=...)`
fournit les ports `lands(repo, head)`, `diagnoses(repo, head)` et
`links_for(repo, head)`. `path` contient les objets Git publiés ; le lecteur
ne récupère aucune branche et ne touche pas au checkout. Il relit le manifeste
SQL par la clé commune, vérifie source/cible, arbre, parents multiparents et
identités des admissions dans les preuves persistées. Une contradiction
refuse le relevé ; un manifeste absent peut utiliser le lecteur ordinaire
`fallback`, dont le traitement des paquets reste prudent.

Chaque membre porte uniquement les fichiers de son diff depuis **sa base de
divergence avec `plan.base`**, sans résolution heuristique ni diff agrégé. Un
historique superficiel ou plusieurs bases de divergence sont refusés. Les
noms d'auteur Git restent absents ; #1681 cherche séparément l'assigné du lot
candidat. Une branche de lot déplacée après fusion n'altère pas ces faits.

`impact(repo, head)` est un fournisseur qualifié d'artefact mesuré, explicitement
injecté. Le lecteur contrôle `ameesh-carte/1` et le commit couvert exact avant
de déléguer à `incident.links_of`. Un artefact absent, périmé ou étranger rend
`None`, donc `unknown`. Il ne sélectionne aucun chemin local, téléchargement
ou carte Nexlink. Le callback contextualisé `links_for` évite de transmettre
la même carte à deux dépôts ou à plusieurs têtes. Le rapport commun est rendu
seulement si ses cinq champs de paquet correspondent au manifeste exact.

`train_incidents.consume(db, passages, consumer=incident)` route ces trois
ports par dépôt vers les passages activés explicitement fournis. Une liste
vide conserve exactement l'appel `incident.consume(db)`. Les autres dépôts
gardent le lecteur ordinaire d'atterrissage et une carte inconnue. Le
`Passage.incident_facts` construit son lecteur sur la connexion de l'appel,
sans charger le canon, le runtime ni son historique courant de qualification.

Le raccord #1681 est fourni par deepseek4 le 15 octobre à 17:19 sur
`6a66ab16729fd3143ce6b4d6a75d3896707d34cf`. Le témoin interbranches charge
`incident.py` et extrait par AST la méthode exacte `Runner.incident_once` du
même commit, puis exerce les ports du consommateur et son point d'appel :

```sh
PYTHONPATH=src:. .venv/bin/python reviews/1467-diagnostics-contract/probe.py \
  --supplier . --commit 6a66ab16729fd3143ce6b4d6a75d3896707d34cf
```

Les six témoins sont verts avec Git réel, stockage et provenance de carte
simulés. La chaîne `incident_once → train_incidents.consume → incident.consume`
ouvre deux incidents, attribue seulement celui du dépôt couvert et garde
`unknown` pour l'autre, sans double appel du consommateur. Les ports reçoivent
la connexion de l'appel, le dépôt et la tête attendus.
Ils ne rejouent pas la suite du fournisseur. Les sept tests du lecteur sont
dans `tests/test_1467_train_incidents.py`. Le témoin SQL/Git avec connexion
fraîche est
`test_1467_train_runner.TrainRunnerTest.test_post_landing_facts_read_sql_manifest_on_fresh_connection_without_ci`.
Le dernier raccord dans `incident_once` est :
`train_incidents.consume(db, getattr(self, 'train_passages', ()), consumer=incident)`.
Il appartient à la branche du fournisseur #1681 ; les lecteurs seuls ne
constituent pas une activation ni une sélection de carte qualifiée.
