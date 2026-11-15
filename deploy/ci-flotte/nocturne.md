# Suite psql nocturne

Le déclenchement quotidien vient de `ameesh-psql-nocturne.timer`, à 01:47 UTC,
sur les candidats du rôle élu `mesh-design`. Installer et vérifier avec
`ameesh role timers --install` et `ameesh role timers --check` après adoption
de la version. Le service ne dispatche que depuis le détenteur du bail vivant.
Il suit les dépôts et branches de `github.projects` qui portent
`psql-nocturne.yml` ; aucune cible propre à un client n'est codée en dur.
Le cron GitHub est retiré : ses retards de six à huit heures recoupaient la CI
de paquet du matin.

Trois admissions utilisent la même fenêtre UTC [00:00, 06:00) : le dispatcher,
le job hébergé `fenetre`, puis chaque part psql après attribution du runner.
Un rattrapage systemd hors fenêtre est journalisé sans dispatch. Un workflow
retardé saute la suite avec une annotation et un résumé ; une part retardée
échoue explicitement comme suite incomplète. Une erreur d'exécution du garde
échoue, elle n'est pas transformée en admission ni en saut normal.
Chaque part reste bornée à 60 minutes : une part admise avant 06Z libère son
runner avant 07Z, loin des coupures de staging.

Le nocturne n'exécute que quatre parts psql simultanément, sur seize parts
au total. Sur les seize runners observés lors de l'incident, douze places
restent donc disponibles pour le paquet dès que ses jobs sont prêts ; les
courts jobs de préparation et d'agrégation gardent leurs plafonds existants.
Ce plafond ne garantit pas la capacité d'une flotte dont seuls quatre runners
ou moins seraient disponibles. Les paquets gardent leur parallélisme normal.

Les échecs restent visibles : `fail-fast: false`, aucun appel d'annulation,
et `cancel-in-progress: false` sur le groupe par référence. Le prochain
nocturne attend le précédent. Une exécution différée ne produit aucune preuve
de suite complète et ne change pas les résultats d'une exécution antérieure.
Pour rejouer un rouge, dispatcher `psql-nocturne.yml` sur la référence voulue
dans la même fenêtre ; le dispatch manuel garde aussi les admissions.

La sémantique du plafond est celle de
[`strategy.max-parallel`](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax#jobsjob_idstrategymax-parallel).
Le correctif ne modifie aucune exécution déjà en cours.
