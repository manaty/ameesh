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
| #1651 : résolution qualifiée `0cd883bc8cdd`, tête brute en conflit | La sélection doit conserver la remise qualifiée et son contexte. Un rejeu brut en conflit expose actuellement le lot, la base intermédiaire, le gel et les fichiers ; aucun transport n'est encore sélectionné par ce raccord. | Test Git de conflit avec contexte exact et absence de publication. Port de transport restant à raccorder ci-dessous. |

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

## Port de transport à raccorder

`Repository.compose` appelle encore `land.merge` sans résolution de paire.
Le reçu de transport doit permettre au consommateur de vérifier la source,
la cible, la base de composition, le lot et son gel d'origine, puis le
commit et l'arbre remis, avec les preuves acquises sur le contenu et les
adaptations éventuelles. Une note de GO ou une référence Git seule ne décrit
pas cette liaison. Un ancien enregistrement `work_resolutions` ne prouve pas
à lui seul le delta sur la base courante.

Le raccord doit réutiliser les qualifications déjà acquises et distinguer
une remise mécanique d'une modification de contenu ; cette documentation
n'impose pas de nouvel avis sur chaque transport. Il devra persister la
preuve avec le manifeste et la revalider avant les effets, sans substituer
la tête remise au gel source d'une génération existante.

L'API et le reçu qualifiés ont été demandés à mesh-design dans le fil #1467.
Les transports courants restent chez impl01, selon le courrier #69382.
Le refus détaillé et les témoins de ce dossier ne valent pas consommation
automatique de #1651 ni activation du train.
