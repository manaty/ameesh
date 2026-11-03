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
rapport nomme le `PermissionError`. Vérifier ce point sur la première passe.

Le budget de compression est de 128 fichiers / 120 secondes par passe. La
compression écrit des membres gzip complets et, après chaque bloc, un état
`<journal>.gz.state` : une passe qui atteint le budget laisse un
`<journal>.gz.part` valide et son offset, et la passe suivante **reprend**
au lieu de repartir de zéro. Un `.gz.part` sans état lisible (résidu d'un
processus tué) est jeté puis recompressé ; il ne bloque jamais la rotation.
Un suffixe postérieur au dernier état enregistré est coupé avant reprise.
Le rapport dit les fichiers déjà traités, les compressions en cours, les
résidus ignorés et le budget atteint. Une erreur réelle, une source modifiée
ou une archive existante conserve le log original et enlève le partiel.
Le service a sa propre borne de 15 minutes. Le ménage docker garde les
politiques existantes de #5325 ; les images étiquetées sans conteneur ne
sont pas élaguées par cette passe (le workflow quotidien s'en charge).

## Surveillance via GitHub, sans identifiant nouveau sur les VM

Le workflow `ci-disque.yml` s'exécute chaque heure et cible chacune des trois
VM par son **label de machine**. Le bureau doit d'abord ajouter le label
`ameesh-ci-1`, `ameesh-ci-2` ou `ameesh-ci-3` aux runners de la VM correspondante.
Le nom du runner ne constitue pas automatiquement un label. Une matrice de
slots sans affinité ne garantit pas de visiter chaque VM.

La commande `scripts/ci-disque.py mesure --host <label-machine>` ne fait qu'un
`statvfs` et publie une notice `ameesh-ci-disk/1` contenant hôte, volume,
octets libres, total et pourcentage utilisé. Elle publie aussi au vert pour
permettre la résolution de l'alerte. Chaque job est borné à 3 minutes ; le
service de rétention reste indépendant de ce workflow.

Le veilleur GitHub d'ameesh lit les annotations via son accès existant,
uniquement dans ce workflow sur la branche de base, jamais sur les PR. Il
conserve dans `ci_disk_state` (migrations additives 0539 et 0540) le dernier
relevé par dépôt, VM et volume. Appliquer les migrations **avant** de mettre
à jour le veilleur. Une vieille exécution ne peut pas remplacer un relevé plus
récent. Une lecture ratée ne crée pas de faux relevé sain.

`ci_disk_low` utilise `resources.disk_warnings`, donc les seuils des fiches
Host du mesh (défauts 85 % utilisés ou moins de 15 Gio libres). Vérifier que
les noms Host correspondent aux labels du workflow. Une sonde absente ou
vieille de plus de deux heures lève `ci_disk_unreadable`. Avec la sonde
horaire à :17, une seule exécution manquée peut déclencher cette alerte. Une
VM vue pour la première fois (`expect_disk`) a **deux heures de grâce** après
sa **première** apparition (`first_seen_at`, stable) avant que l'absence de
première mesure n'alerte : la grâce ne se renouvelle pas à chaque collecte,
donc une VM qui reste dans les jobs sans jamais produire de sonde alerte bien
au bout de deux heures. Une mesure déjà reçue puis devenue vieille alerte
toujours. Un dernier relevé bas reste signalé même quand il devient ancien ;
seul un relevé sain plus récent résout le rouge. Les alertes ont une identité
stable par VM et volume, indépendante du numéro de run.

Une VM retirée de la matrice du workflow (labels) cesse d'apparaître dans les
jobs lus. Sa ligne est **oubliée après 24 h** sans réapparition, avec ses
alertes. La grâce lit `first_seen_at` (première apparition, stable) ;
l'oubli lit `seen_at` (dernière apparition, rafraîchie à chaque job vu).
Une lecture GitHub ratée n'appelle pas `collect`, donc n'oublie rien ; un job
encore en file repousse la date de dernière apparition, et un simple échec de
sonde garde les dernières mesures basses au lieu de les effacer. Rien n'est
oublié tant qu'aucun nom de job n'a été lu.

Ne pas installer `hosts --publish`, de DSN central ou de clé SSH nouvelle
sur les VM CI pour cette surveillance (direction mesh-design, 03/11/2026,
messages #187442 et #187450). Les jobs de PR partagent ces machines.

## Qualification et retour

Avant et après pose, relever `df -B1 /`, `df -i /`, les tailles `_diag`,
`_work/_temp`, caches et `/tmp` (rapport `--complet`), ainsi que
`docker system df`. Conserver les preuves au lot, pas dans ce runbook.
Répéter après une journée de CI ; comparer les octets libres et l'évolution
des catégories, sans attribuer une croissance aux logs sans mesure.

Après intégration, dispatcher `ci-disque.yml` sur la branche de base et
vérifier trois jobs et trois relevés, puis les alertes et leur notification.

Pour tester le trajet d'alerte sans remplir le disque : relever d'abord les
seuils de la fiche Host, augmenter temporairement `warn_disk_free` au-dessus
du libre réel d'une VM, dispatcher la sonde sur la base et constater `ci_disk_low`
et sa notification dans le mesh. Restaurer immédiatement les valeurs exactes
et dispatcher à nouveau. Répéter sur chaque VM ; ne pas changer les seuils
`min_disk_free` qui gouvernent la capacité. Ce témoin et le relevé à J+1 sont
des gestes du bureau, après intégration de la configuration revue.

Retour rétention :
`sudo bash deploy/ci-flotte/install-disque.sh --remove`. Cela arrête la passe
avant de retirer ses fichiers et conserve la configuration et les archives.
Les jobs GitHub continuent leur ménage habituel. La sonde GitHub est
indépendante : ne la désactiver qu'avec une surveillance de remplacement.
