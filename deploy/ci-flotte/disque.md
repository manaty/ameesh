# Rétention et surveillance du disque des VM CI

Les jobs utilisent `scripts/ci-disque.py` (#5325). Leur annotation GitHub
n'est pas une notification du mesh. Le workflow quotidien `menage-disque.yml`
utilise des slots libres : plusieurs slots peuvent tomber sur une même VM.
La rétention doit donc aussi tourner **sur chaque VM**, indépendamment du
planificateur GitHub et même quand un job échoue avant son checkout (#5630).

## Pose par le bureau

Depuis un checkout revu, sur chacune des VM CI :

1. Adapter `ameesh-ci-disque.env.example` au workspace réellement présent,
   puis le poser en `/etc/ameesh-ci-disque.env` (root, 0644, aucun secret).
   Le compte de l'unité est `runner` ; si le compte des runners diffère,
   adapter `User` et `Group` par un drop-in systemd.
2. `sudo bash deploy/ci-flotte/install-disque.sh --install`
3. `sudo bash deploy/ci-flotte/install-disque.sh --check`
4. `sudo systemctl start ameesh-ci-disque.service`, puis lire
   `journalctl -u ameesh-ci-disque.service -n 100 --no-pager`.

La passe partage le verrou des jobs et s'exécute sous leur uid, jamais root.
Elle ne touche pas aux caches d'actions, d'outils et d'updaters. Les temporaires
sous `_work/_temp` et `/tmp` gardent leur garde au **ctime >48 h** ; `/tmp`
est borné au compte courant. Le contenu `_work` hors `_temp` est conservé.
Les journaux `Runner_*.log` et `Worker_*.log` fermés sont compressés après
72 h ; les archives restent encore 7 jours. Les liens sont ignorés. Les
inodes ouverts par Listener/Worker sont gardés même s'ils sont anciens ;
si leurs descripteurs sont illisibles, la rotation est abandonnée et le
rapport l'indique. Vérifier ce point sur la première passe.

Le budget de compression est de 128 fichiers / 120 secondes par passe.
Une erreur, une source modifiée ou une archive existante conserve le log
original. Le service a sa propre borne de 15 minutes. Le ménage docker garde
les politiques existantes de #5325 ; les images étiquetées sans conteneur
ne sont pas élaguées par cette passe (le workflow quotidien s'en charge).

## Surveillance native du mesh

Utiliser l'échantillonneur existant `ameesh hosts --publish` (#2022/#5559),
qui fonctionne sans runner d'agent et publie sous `publish@<hôte>`. Ne pas
remplacer cette surveillance par les annotations des jobs.

Prérequis à préparer par le bureau avant activation : compte de service
`ameesh`, installation versionnée sous `/opt/ameesh/venv`, configuration
individuelle de l'hôte et accès à la base suivant le mécanisme de secrets
habituel. Aucun DSN dans le checkout, dans le compte `runner` ou dans les
journaux. La disponibilité de ces prérequis sur les VM CI doit être vérifiée
par le bureau ; le script de rétention ne les crée pas.

Poser et activer les unités existantes
`deploy/serveur/ameesh-releve-hote.{service,timer}` sur **chaque VM**. Vérifier
la fiche Host (nom identique à config.json, volumes des runners inclus) et
les seuils `warn_disk_used` / `warn_disk_free`. Sans surcharge, le mesh
avertit à 85 % utilisés ou sous 15 Gio libres ; la tendance est aussi
calculée sur les relevés. Lire `ameesh hosts --fleet` et les alertes : le
relevé doit être frais et porter `publish@<hôte>`. Un échantillonneur muet
est signalé par `host_unsampled`, y compris pour un hôte sans agent.

## Qualification et retour

Avant et après pose, relever `df -B1 /`, `df -i /`, les tailles `_diag`,
`_work/_temp`, caches et `/tmp` (rapport `--complet`), ainsi que
`docker system df`. Conserver les preuves au lot, pas dans ce runbook.
Répéter après une journée de CI ; comparer les octets libres et l'évolution
des catégories, sans attribuer une croissance aux logs sans mesure.

Pour tester le trajet d'alerte sans remplir le disque : relever d'abord les
seuils de la fiche Host, augmenter temporairement `warn_disk_free` au-dessus
du libre réel d'une VM, publier un relevé et constater `host_disk_soon_full`
et sa notification dans le mesh. Restaurer immédiatement les valeurs exactes
et publier à nouveau. Répéter sur chaque VM ; ne pas changer les seuils
`min_disk_free` qui gouvernent la capacité. Ce témoin et le relevé à J+1 sont
des gestes du bureau, après intégration de la configuration revue.

Retour rétention :
`sudo bash deploy/ci-flotte/install-disque.sh --remove`. Cela arrête la passe
avant de retirer ses fichiers et conserve la configuration et les archives.
Les jobs GitHub continuent leur ménage habituel. L'échantillonneur est
indépendant : ne le désactiver qu'avec une surveillance de remplacement.
