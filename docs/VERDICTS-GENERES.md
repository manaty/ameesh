# Report d’avis sur des sorties générées

Le lot #7323 (manaty/ameesh#5527, demande mesh-design du 14 novembre 2026)
étend le report d’avis favorable aux seules sorties déclarées dans
`.ameesh/generated-files.json`. La déclaration est lue sur la tête intégrée
de la cible, jamais depuis le checkout courant ou la branche candidate.

La nouvelle tête descend de la tête relue par des commits linéaires limités
à ces sorties. Les sources, générateurs, déclarations et autres fichiers
restent identiques à ceux relus ; une modification de code, même rétablie
ensuite, refuse le report. Le manifeste ne peut pas se déclarer comme sortie.
Les sorties sont des fichiers ordinaires.

Chaque commande `check` déclarée est exécutée sans shell sur un checkout
jetable de la tête gelée. Elle doit sortir en zéro sans retoucher sa sortie.
Un contrôle rouge, absent, expiré ou non concluant refuse le report. Le délai
par contrôle est de 60 secondes. Aucun paquet n’est installé automatiquement.

Un `runtime` facultatif permet de réutiliser les outils locaux installés ;
les fichiers `locks` déclarés doivent être identiques aux blobs gelés.
Le clone radar reçoit ce runtime depuis le dépôt local d’origine, pas depuis
son propre checkout. L’environnement est construit par liste fermée : PATH
système, HOME et TMPDIR jetables, locale C.UTF-8, PYTHONPATH vers les sources
gelées et PYTHONNOUSERSITE=1. Aucune autre variable de l’hôte n’est héritée
(notamment NODE_OPTIONS, NODE_PATH, PYTHONHOME et LD_PRELOAD).

Un exécutable nommé sans chemin est résolu avec le PATH de l’hôte, puis
lancé par son chemin absolu dans cet environnement fermé. Cela permet par
exemple le Node installé hors du PATH système, sans transmettre le PATH de
l’hôte au contrôle.

La preuve fait encore confiance aux exécutables locaux et aux octets des
dépendances locales : comparer les locks ne certifie pas ces octets. Elle
suppose un hôte et un runtime locaux de confiance ; ce n’est pas une exécution
distante attestée ni un bac à sable contre un runtime compromis. Tous les
contrôles déclarés, même ceux des sorties inchangées, doivent passer.

Seuls les avis favorables sont reportés, comme pour les adaptations de tests.
Les anciens refus « differs outside a test or a conflict » sont réexaminés
une fois sur le même gel pour essayer cette preuve nouvelle.
