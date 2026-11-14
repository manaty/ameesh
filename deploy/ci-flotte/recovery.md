# Récupération des services de runners CI

`install.sh --install` installe et active le timer
`ameesh-ci-runner-recover.timer`. Toutes les minutes après la fin de la passe
précédente, il examine les services natifs `actions.runner.*` chargés par
systemd. Aucun identifiant GitHub n’est requis.

Le récupérateur démarre uniquement un service `failed` dont le cgroup ne
contient plus de processus. Un listener tué peut laisser un Worker vivant
(`KillMode=process`) : dans ce cas, le service reste intact et une erreur est
journalisée. Les services actifs, en transition, ou arrêtés volontairement
(`inactive`) ne sont pas démarrés. Un job systemd déjà en attente interdit
la reprise ; `--job-mode=fail` ne remplace pas un arrêt en attente. Pour une
maintenance, arrêter le timer et le service de récupération avant de manipuler
les unités ou de lancer `ameesh-ci-runner-drain --all --stop --wait 3600` :
`sudo systemctl stop ameesh-ci-runner-recover.timer ameesh-ci-runner-recover.service`.
Arrêter aussi le service empêche une reprise déjà lancée de continuer. Réactiver
ensuite le timer avec `sudo systemctl start ameesh-ci-runner-recover.timer`,
y compris si la maintenance est annulée. Après un reboot, le timer activé au
démarrage reprend automatiquement.

Le verrou `/run/lock/ameesh-ci-runner-drain.lock` est partagé avec le drain.
La récupération ne peut donc pas se glisser entre son `cgroup.kill` et son
arrêt/redémarrage. **Conserver `Restart=no` sur les runners** : la preuve du
drain gelé dépend de l’absence de redémarrage autonome. Le timer assure la
résilience sans modifier cette propriété. Il ne réinitialise pas les limites
de démarrage systemd ; les tentatives sont espacées d’au moins une minute.

L’installation recharge systemd et active les timers ; elle ne signale et ne
redémarre aucun runner actif. `install.sh --check` compare les fichiers et
vérifie les deux timers. `--remove` désactive les timers et retire leurs
fichiers sans arrêter les runners.

## Surveillance

`journalctl -u ameesh-ci-runner-recover.service -p err` expose les services
indisponibles, avec `Result`, `ExecMainCode` et `ExecMainStatus` avant toute
reprise, ainsi que les échecs de récupération. Le service de surveillance
sort en échec si une reprise ne peut pas être faite sans risque ; son timer
retente à la passe suivante. Les reprises réussies et les arrêts conservés
sont journalisés au niveau notice. Surveiller également l’activité du timer
avec `install.sh --check` : une surveillance désactivée ne produit pas de
nouvelles alertes.

Cette sonde détecte l’indisponibilité **du service local**. Un listener actif
mais déconnecté de GitHub demande toujours le relevé des runners côté GitHub ;
le récupérateur ne tue aucun processus pour corriger une panne réseau.

## Qualification sans job

Utiliser une unité de réserve sans job ni credentials GitHub, sous un nom
`actions.runner.*.service`, avec `Restart=no` et un processus d’attente.
Limiter la liste du banc à cette seule unité : ne jamais tuer un runner de
la flotte pour la démonstration. Vérifier successivement :

1. Un SIGKILL produit `failed`, `Result=signal`, `ExecMainStatus=9`.
2. Une passe du récupérateur ramène cette unité à `active` et journalise
   l’échec puis la reprise.
3. Un `systemctl stop` la laisse `inactive` après plusieurs passes.
4. Arrêter et nettoyer l’unité de réserve.

Le banc unitaire vérifie en plus qu’un cgroup peuplé, un état illisible, un
job systemd en attente ou un arrêt observé pendant la lecture empêchent la
reprise. Avant activation sur une VM native, refaire cette qualification
sur sa réserve et vérifier les journaux, puis le statut online côté GitHub
lors de la prochaine reprise naturelle d’un runner.
