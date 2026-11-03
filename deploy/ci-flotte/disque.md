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
Elle ne touche pas aux caches d'actions ni d'outils. Pour `_work/_update`
(#5635), elle ne retire que l'extraction d'un auto-update **déjà installé ou
dépassé**, après preuve que le cache est hors usage (voir ci-dessous). Les
temporaires sous `_work/_temp` et `/tmp` gardent leur garde au **ctime >48 h** ;
`/tmp` est borné au compte courant. Le contenu `_work` hors `_temp` et hors
cache `_update` éligible est conservé. Les journaux `Runner_*.log` et
`Worker_*.log` fermés sont compressés après 72 h ; les archives restent encore
7 jours. Les liens sont ignorés. Les inodes ouverts par Listener/Worker sont
gardés même s'ils sont anciens ; si leurs descripteurs sont illisibles, la
rotation est abandonnée et le rapport nomme le `PermissionError`. Vérifier ce
point sur la première passe.

Le budget de compression est de 128 fichiers / 120 secondes par passe. La
compression écrit des membres gzip complets et, après chaque bloc, un état
`<journal>.gz.state` : une passe qui atteint le budget laisse un
`<journal>.gz.part` valide et son offset, et la passe suivante **reprend**
au lieu de repartir de zéro. Un `.gz.part` sans état lisible (résidu d'un
processus tué) est jeté puis recompressé ; il ne bloque jamais la rotation.
Un suffixe postérieur au dernier état enregistré est coupé avant reprise.
Les auxiliaires de reprise ne suivent aucun lien : `.gz.part` et `.gz.state`
sont ouverts par descripteur (`O_NOFOLLOW | O_NONBLOCK`), l'état enregistre
l'inode du partiel, et le temporaire d'état est créé exclusivement sous un nom
imprévisible (`mkstemp`). Un lien planté sous ces noms est retiré, sa cible
reste intacte ; une FIFO plantée sous ces noms est refusée sans attendre
d'écrivain.
Le rapport dit les fichiers déjà traités, les compressions en cours, les
résidus ignorés et le budget atteint. Une erreur réelle, une source modifiée
ou une archive existante conserve le log original et enlève le partiel.
Le service a sa propre borne de 15 minutes. Le ménage docker garde les
politiques existantes de #5325 ; les images étiquetées sans conteneur ne
sont pas élaguées par cette passe (le workflow quotidien s'en charge).

## Caches `_work/_update` des auto-updates (#5635)

Le mécanisme officiel du runner (source `actions/runner`,
`Runner.Listener/SelfUpdater*.cs`) télécharge et extrait le paquet dans
`_work/_update`, copie `bin` → `bin.<version>` et `externals` →
`externals.<version>` à la racine, puis bascule les liens `bin`/`externals`
avec `_work/_update.sh`. Il supprime `_work/_update` au **début** du cycle
suivant, pas à la fin du courant : sans ménage, chaque slot garde une
extraction d'environ 700 MiB (mesure opérateur du 03/11 : ~699 000 KiB ×
8 slots ≈ 5,33 GiB/hôte).

La politique ne retire un cache que si toutes les preuves suivantes tiennent :

* chemin attendu : `<home>/rN/_work/_update`, racine avec `.runner`, aucun
  lien dans le chemin, `_update` réel et sur le même système de fichiers ;
* entrées de tête attendues du paquet ; `bin` et `externals` réels (jamais
  des liens) ; liens internes du paquet autorisés seulement s'ils restent
  dans le cache (les liens `node*/bin/npm` du paquet officiel sont internes) ;
* version du cache lue dans `bin/Runner.Listener.deps.json`, sans exécuter le
  binaire : version **inférieure ou égale** à `bin/Runner.Listener.deps.json`
  courant. Une version supérieure est une mise à jour en attente, préservée ;
  une version illisible aussi ;
* pour la version égale, `bin.<v>`/`externals.<v>` installés et les liens
  courants cohérents, avec la même empreinte de `Runner.Listener.deps.json` ;
* aucun processus du même uid avec `cwd` ou descripteur ouvert sous le cache
  (updater vivant), aucun script `_work/_update.sh` vivant, aucun
  `Runner.Worker` rattaché à cette racine, aucun Listener redémarré depuis
  moins de 10 min (unité réactivée entre deux contrôles) ; le rattachement du
  Worker se fait par son `cwd` réel — `rN`, `rN/bin`, `rN/bin.<version>` ou
  `rN/_work/...` (relevé opérateur du 03/11 : un Worker tourne depuis
  `rN/bin`) — ou, si le `cwd` est illisible, par le chemin de son exécutable ;
  un Worker d'une autre racine ne bloque que la sienne ; une attribution
  impossible (cwd hors `home`, `home` lui-même, cwd et exécutable illisibles)
  préserve le cache ; un shell dont la cmdline ou les descripteurs sont
  illisibles préserve aussi (updater potentiel) ; un zombie ne compte pas ;
* `bin`, `externals`, `bin.*` et `externals.*` de la racine ne pointent pas
  dans le cache. Les versions installées et les sauvegardes `bin.*`/
  `externals.*` ne sont jamais touchées.

Le contrôle est refait juste avant la suppression (inode, version, processus,
liens). Un doute, une erreur de lecture ou une suppression partielle sont
rapportés dans la ligne de ménage, sans faire rougir l'étape. Aucun label
n'est retiré, aucune mise à jour n'est désactivée, aucun identifiant nouveau
n'est posé sur les VM.

### Qualification et marge disque

Après intégration, réinstaller la copie exécutée par la minuterie
(`sudo bash deploy/ci-flotte/install-disque.sh --install`), puis sur chaque VM
et sans drain si le dry-run est propre :

1. `sudo -u runner /usr/local/lib/ameesh-ci/ci-disque.py menage --force
   --dry-run` : lire les décisions `rN` (retiré / préservé / refusé et la
   raison), vérifier qu'aucun updater ni Worker actif n'est préservé à tort.
2. Relever `df -B1 /`, `df -i /` et la taille de chaque `_work/_update`
   (`rapport --complet` publie désormais une ligne `cache _update rN`).
   Lancer la passe réelle, puis vérifier que `bin`, `externals`,
   `bin.<v>`/`externals.<v>` et les liens courants sont intacts, et que
   `df -B1 /` a gagné au moins la taille mesurée des caches retirés.
3. Attendre deux cycles d'auto-update (ou deux mises à jour provoquées par
   le mécanisme officiel) et refaire les mêmes relevés : un cache dont la
   version vient d'être installée doit partir ; un cache de version
   supérieure doit rester jusqu'à la bascule.
4. Si un drain est nécessaire, réutiliser `ameesh-ci-runner-drain` (#3312) et
   ne redémarrer que les unités que cette opération a arrêtées.

Arithmétique de capacité, à ne pas confondre avec la prévention : après le
retrait opérateur des 23 caches, les VM avaient 6,6–7 GiB libres, sous le
seuil mesh de 15 GiB. Cette politique supprime le pic durable de 5,33 GiB,
mais ne garantit pas 15 GiB pendant la fenêtre d'un update sur une racine de
35 GiB. Si le seuil ne tient pas après une journée de mesure, chiffrer la
capacité avant toute dépense (ordre de grandeur : +8 à +9 GiB, ou une racine
de 48 GiB) et demander la décision owner.

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
