# Candidature et admission au train

Le composeur provisoire et le train n'ont pas encore le même point d'entrée.
`~/Work/outils/compose-paquet.py` lit directement les lots `qa` par SQL ;
`train_admissions.Admissions` lit les générations inscrites dans la file.
Le contrat de candidature existant est `plan_git.freeze_candidate`, livré
par #1756 au commit `53382b8a7a023ee7294d8a87add28afa4b582370`.
Ce lecteur doit servir à l'inventaire des candidatures ; il ne constitue
ni une preuve historique de relecture, ni une écriture de gel ou de file.

Les cas signalés par mesh-design le **16 octobre 2026 à 02:44**, courrier
**#69382** et note du parent #1606, donnent les frontières suivantes :

| Cas | Diagnostic et comportement attendu | Couverture consommateur |
|---|---|---|
| #753 : ancien gel `d4c2bfc2`, GO sur `b18f40ad` | #1756 expose `declared-freeze-stale` et le candidat du verdict. Le train confronte toujours le gel de la génération admise à la branche publiée ; un refus nomme chaque lot, sa branche, le gel et la tête publiée. | Probe interbranches, puis test Git de deux branches déplacées. |
| #1734 : classe légère sans avis | La politique versionnée peut exiger zéro avis préalable. L'absence d'avis n'enlève pas la candidature. Les auteurs historiques restent connus et un blocage explicite reste bloquant. | Parcours politique → admission SQL → historique Git avec zéro avis, plus refus sensible et blocage léger. |
| #1651 : résolution qualifiée `0cd883bc8cdd`, tête brute en conflit | Le port explicite consomme la remise qualifiée sur sa base exacte et conserve son parent. Une remise incompatible est reportée avec son motif et ses dépendants ; les autres membres peuvent composer leur paquet. | Git réel : tête brute conflictuelle, reçu accepté, mauvais contexte refusé, reprise du même plan et maintien du parent. La remise réelle de #1651 reste chez le composeur. |

Les états de #753 et #1734 ont été régularisés par le pilote ; ces numéros
désignent les régressions à prévenir, pas un relevé courant de leur admission.
La matrice ne modifie pas le paquet en cours ni les transports du composeur.

## Raccord de #1756

Le probe charge uniquement l'objet `plan_git.py` du commit fournisseur exact,
sans fusion de branche. Il éprouve quatre parcours de ce consommateur :
gel déclaré périmé, gel déclaré absent, léger sans avis et nouveau gel après
un avis sur une autre tête. Les deux premiers sont visibles par le lecteur
de candidature, mais ne peuvent remplacer implicitement une génération de
file ni les contrôles du lecteur historique. Le probe ne rejoue pas les
tests du fournisseur.

```sh
PYTHONPATH=src:. .venv/bin/python reviews/1467-selection-contract/probe.py \
  --supplier . --commit 53382b8a7a023ee7294d8a87add28afa4b582370
```

Git est réel ; jalons, stockage et provenance sont des fixtures explicites.
Le raccord de l'inventaire `qa` à ce lecteur reste distinct de l'admission
historique : aucune seconde dérivation de gel n'est ajoutée au train.

## Port de transport qualifié

Source de ce raccord : cadrage mesh-design **#69852 du 16 octobre à 03:12**,
persisté dans #1467. Aucun nouveau catalogue, formulaire de GO ou gel n'est
créé. Le fournisseur explicite lit les qualifications déjà acquises dans le
manifeste/journal du composeur ; une note ou une ref seule reste insuffisante.
Les résolutions de paire de `land.merge` ne sont pas ce contrat.

`Repository(..., transports=reader)` reçoit un lecteur de confiance appelé
avec `source`, `target`, `row`, `base`, `path` et `expected`. `base` est la
tête intermédiaire exacte ; `expected` est le reçu persisté lors d'une
revalidation, sinon `None`. `None` en retour signifie aucune remise disponible.
Le lecteur doit vérifier la qualification et son maintien depuis les preuves
existantes ; le consommateur vérifie ensuite les objets Git et le delta.
La forme `ameesh-train-transport/1` comprend :

- `source`, `target`, `admission` : l'identité complète de génération/gel,
  inchangée et séparée de la remise ;
- `base`, `head`, `tree` : commits et arbre complets ; la base doit être le
  premier parent de la remise, et le gel source doit rester son ancêtre ;
- `delta` : SHA-256 des enregistrements Git bruts de `base..head`, chemins
  triés et délimités par NUL, modes et identifiants complets des objets. Ils
  lient aussi les contenus binaires, sans dépendre des couleurs, préfixes,
  algorithmes de hunks ou de l'ordre local. Aucun renommage, outil externe,
  textconv ou remplacement d'objet ;
- `ref` : emplacement sous `refs/ameesh/resolutions/<lot>/`, utilisé pour
  récupérer l'objet absent dans le clone privé, jamais pour déduire un avis ;
- `qualification` : `kind=mechanical|adapted` et `references` structurées
  vers des jalons, courriers, événements existants (`kind` et `id`), ou un
  manifeste Git (`kind=manifest`, `commit`, `path`). Le lecteur résout ces
  références ; leur forme seule ne prouve pas leur autorité.

Le reçu est enregistré dans `plan.transports`, en dehors de `plan.proof`
qui conserve l'admission historique. L'empreinte du plan et l'autorité de
promotion couvrent ce champ. Le commit de paquet garde les transports dans
ses parents et lie leur empreinte dans son message. Les commits intermédiaires
propres ont des identités déterministes : une reconstruction peut revalider
la même base, même après perte du clone et changement d'horloge.

Le fournisseur est relu avant les effets. Une remise retirée ou remplacée
refuse la promotion ; sa présence dans un plan ne constitue pas une nouvelle
autorisation. Une remise mécanique conserve ses qualifications acquises,
sans re-gel ni demande de nouvel avis. Une remise adaptée conserve les
références qui qualifiaient déjà son contenu sur cette base.

Pendant la composition, une remise incompatible ou un conflit brut produit
un report nommé dans `state.selection` et `state.deferred`, avec ses dépendants.
La file d'admission n'est pas éjectée et aucun lot n'est déclaré fautif.
La composition des membres restants précède l'unique CI ; si tous sont reportés,
le résultat est un paquet vide avec ses motifs et aucun effet distant.

Le lecteur d'incidents accepte les parents supplémentaires vérifiés et utilise
le diff qualifié pour les fichiers d'un membre transporté, en conservant son
lot et son gel source. Il n'attribue pas une cause ou un auteur à la résolution.

`Passage(..., transports=reader)` lie le lecteur à la connexion fraîche du
passage (`reader(db, **context)`). Aucun lecteur réel n'est choisi par défaut.
Les transports courants restent chez impl01 ; le format de leur manifeste
est précisé par sa réponse du **16 octobre à 03:40** : aucun fichier de reçu
structuré dédié n'est versionné aujourd'hui. Les faits Git sont portés par
les commits à deux parents (base de chaîne, remise auteur), leurs messages et
les refs `refs/ameesh/resolutions/`. Le journal courant
`.qa/paquet-23h/MANIFESTE-chaine-paquet-23h-suivant.md` n'est pas versionné ;
ses lignes sont reprises à la coupure dans
`reviews/integration-paquet-h-<timestamp>.md`. Les références de relecture ne
figurent pas dans les commits ou les refs de transport.

Le raccord restant doit donc relier ces faits aux qualifications déjà acquises
dans le reçu durable du train. Le présent port ne lit pas encore ces sources
réelles et ne déduit aucun avis de leurs seuls messages. Il ne demande ni
reconstruction des preuves acquises ni nouvelle relecture d'une remise
mécanique. Ce port préparatoire n'active pas le train.

## Validation du port

Les vérifications ciblées couvrent 27 témoins moteur/Git existants, 7 lecteurs
d'incidents et 15 témoins de transport/sélection. Les nouveaux parcours couvrent
aussi un préfixe propre avant le transport, l'indépendance de l'empreinte aux
réglages locaux de diff et le report nommé d'une lecture d'objet impossible.
Le témoin d'affichage a été rouge avant la canonicalisation du delta, puis vert.

Les 13 témoins Runtime SQL et le nouveau parcours `Passage` sont verts avec
psycopg. Le nouveau parcours est également vert avec psql : plan persistant,
connexion fraîche, clone perdu, même tête promue, un seul essai et conservation
du gel source. Les commandes SQL passent par `ameesh heavy`.

Git et SQL sont réels ; CI, provenance et qualification du composeur sont des
fixtures explicites. Ces preuves ne constituent pas une coupure réelle sans
agent, une qualification des fournisseurs Nexlink ou une activation.
