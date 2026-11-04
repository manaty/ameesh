# Extraits réels du run 41811687787

Source : API GitHub `repos/manaty/ameesh/check-runs/<check_run>`, champ
`output.text`, événements `ameesh-test-event/1`, récupérés le 4 novembre 2026.
Le fichier JSON conserve les événements et leur champ `extrait` sans réécriture ;
seul `expected_disk_infrastructure` est ajouté pour le verdict attendu.
Les neuf check-runs et les liens source sont inclus dans le fichier.

Décision de classement : chaque traceback doit porter sa propre preuve ENOSPC.
Aucune propagation depuis un autre test de la même part. Le `KeyError: 'plan'`
de test_2446, l'IndexError de test_1155, les CalledProcessError sans stderr,
le QueueError « unable to create directory » et les assertions de comparaison
restent négatifs : leur extrait ne prouve pas à lui seul une erreur ENOSPC.
L'assertion de test_3533 reste négative même si le message de porte cite Errno 28.
Un mélange de ces négatifs et d'ENOSPC reste donc négatif pour la part entière.

Les refus « snapshot volume below free-space reserve » sont aussi conservés
comme négatifs dans ce filtre ENOSPC : ils signalent une réserve configurée,
pas une erreur système ENOSPC. Les assertions « does not match » restent des
assertions, et les extraits tronqués ne donnent pas de preuve finale complète.
Ces choix privilégient l'absence de faux verdict infra ; les fixtures rendent
visible cette limite au lieu d'attribuer toutes les erreurs du run au disque.
