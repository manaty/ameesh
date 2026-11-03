# Archives d’actions des VM CI natives

Le bureau applique depuis le checkout fusionné, sur **chaque** VM CI
`ameesh-ci-1/2/3` :

1. `sudo bash deploy/ci-flotte/install-action-cache.sh --install`
2. `sudo bash deploy/ci-flotte/install-action-cache.sh --check`
3. Poser d’abord le drain corrigé de #5656 (`sudo bash
   deploy/ci-flotte/install.sh --check` vert, sinon `--install`), puis
   redémarrer les runners entre deux jobs avec ce drain : il ne coupe pas un
   job accepté avant le lancement du Worker. **Ne jamais utiliser l’ancien
   drain f40978b, suspendu.**
4. Au premier job, vérifier dans les diagnostics Worker la mention
   `Found action archive` et le chemin du cache. `--check` et `systemctl show`
   ne prouvent que les fichiers et la configuration chargée ; le journal du
   nouveau processus prouve l’usage effectif.

Séquence confirmée par le bureau le 03/11/2026 (#187938) : le remplissage et
les drop-ins (étapes 1–2) ne signalent aucune unité ; l’activation et le
redémarrage (étape 3) attendent que #5656 soit relu, fusionné et posé. La
preuve d’usage reste `Found action archive` dans les diagnostics du nouveau
processus, pas `--check`.

L’installateur vise les unités natives
`actions.runner.manaty-ameesh.*.service` présentes sous `/etc/systemd/system`,
et pose pour chacune `action-cache.conf`. Il ne les arrête ni ne les relance.
Le cache `/usr/local/lib/ameesh-ci/action-archives` est publié par root,
commun aux slots et lisible sans écriture par le compte runner. Il utilise
le même format et le même remplissage idempotent que les runners invités.
Aucun identifiant GitHub n’est installé : les archives publiques sont lues
par HTTPS et leurs références sont figées aux SHA des workflows.

Après chaque changement des SHA d’actions, refaire `--install`, puis
`--check` depuis le nouveau checkout. `--check` vérifie la présence et la
somme locale de **tous les SHA actuels**, les drop-ins de toutes les unités
natives et, hors test isolé, l’environnement chargé par systemd. Un SHA
absent est signalé avant de relancer une salve de CI.

Retour arrière : `--remove` retire seulement ses drop-ins, puis le bureau
redémarre entre deux jobs. Les archives restent sur disque pour les lecteurs
en cours et pour une réinstallation ; elles ne donnent aucun droit et ne
sont plus consultées par les nouveaux processus sans la variable.

Les runners invités sur hôtes prêteurs restent couverts séparément par
`deploy/guest/ci-runners.sh --prepare` ; ce chemin ne configure pas la flotte
native et ne remplace pas la procédure ci-dessus.
