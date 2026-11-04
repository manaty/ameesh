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
liens). Le cache est ensuite renommé atomiquement en
`_work/_update.retire-<pid>` (même système de fichiers), puis c'est l'arbre
renommé qui est supprimé : une auto-update qui démarre pendant la suppression
(plusieurs secondes pour ~700 Mio) voit `_update` absent et le recrée, sans
perdre de fichiers de sa nouvelle extraction (#5643). Un doute, une erreur de lecture ou une suppression partielle sont
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

Ne pas installer `hosts --publish`, de DSN central ni de clé SSH nouvelle
sur les VM CI pour cette surveillance. L'interdiction inclut aussi la
réutilisation d'un accès SSH existant : les accès opérateur/root du bureau
ne sont pas des accès de veille automatique. Direction mesh-design du
03/11/2026 (#187442/#187450), confirmée et étendue par sa décision du
04/11/2026, courrier #190059 en réponse à #190057 pour le lot #5751.
Le veilleur passe uniquement par son accès GitHub existant.

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

## Marge avant une part et temporaires de tests (#5742)

Les parts Linux de `tests.yml` (psycopg, psql, autre-fs) et `carte.yml`
passent par `python3 scripts/ci-test-space.py -- <commande de la part>`.
Avant tout import de test, le wrapper vérifie 5 Gio disponibles sur `/`, le
workspace et `RUNNER_TEMP`, et 1 Gio sur le parent temporaire choisi par le
job (dont le tmpfs du banc autre-fs). Chaque volume avec une table d'inodes
bornée doit aussi conserver 10 000 inodes disponibles. Une mesure illisible
ou insuffisante refuse la part avec le code 75 et une annotation « CI non
concluante » : aucun test n'est exécuté, aucun flux vert n'est fabriqué.

La garde mesure la marge au démarrage ; elle ne réserve pas de blocs contre
les autres jobs, les auto-updates ou Docker. Elle arrive après les étapes de
préparation du job et ne peut protéger un Worker qui échoue avant le checkout.
Le rapport et la rétention par VM restent donc nécessaires ; une capacité
insuffisante sous charge exige toujours un dimensionnement de la flotte.

Le wrapper crée un dossier privé, au nom court pour les sockets Unix, dans
le parent temporaire du job. `TMPDIR`, `TMP` et `TEMP` des tests et de leurs
sous-processus pointent dedans. À la fin, succès comme échec, il arrête leur
groupe de processus et enlève ce dossier seul. Une annulation TERM/INT suit
le même nettoyage. La racine autre-fs reste choisie par le workflow ; les
artefacts et les flux dans `RUNNER_TEMP` restent disponibles pour publication.

Cette récupération immédiate complète le ménage de plus de 48 h : elle
évite que les `mkdtemp` abandonnés par les imports ou les tests s'accumulent
entre deux suites. Elle ne supprime ni les temporaires des autres parts ni
ceux écrits explicitement hors de ce dossier. Après SIGKILL ou arrêt brutal
de la VM, le dossier peut rester ; la rétention conservatrice prend le relais.
Ne pas abaisser sa garde d'âge pour récupérer de la place pendant des jobs.

Le bilan requis lit aussi les logs complets des parts Linux en échec.
Un refus explicite de la garde, un Worker mort en ENOSPC ou des tracebacks
exclusivement ENOSPC sont consignés comme infrastructure, sans « négatif
prouvé ». Un autre traceback négatif dans la même part, ou une autre part
en échec, garde le rouge. Un avertissement disque seul ne suffit pas. Les
lectures sont bornées ; un log illisible ou tronqué ne permet aucune
exemption. Le détail reste publié en `neutral` quand seule l'infrastructure
est en cause et que chaque famille conserve une part réussie. Une famille
sans aucune preuve réussie garde la garde de preuve absente (#3449).

### Mesure protégée, compensation du cron et signaux des parts (#5751)

Décision mesh-design du 04/11/2026, courrier #190059 : aucun accès SSH aux
VM pour cette surveillance, même par réutilisation d'une clé du bureau.
Le transport reste le workflow dédié sur la branche de base ; le veilleur
le déclenche par workflow_dispatch lorsque le cron manque son rendez-vous.

Une unité de mesure distincte, ameesh-ci-mesure.service, tourne sous root
toutes les cinq minutes et conserve /var/lib/ameesh-ci-disk/snapshot.json.
Elle ne fait que mesurer le disque : réseau et HOME isolés, système en
lecture seule sauf ce répertoire, capacités retirées, délai de trente
secondes. Le ménage reste sous runner sur son timer de trente minutes.
Aucune clé, aucun compte d'accès entrant ni DSN n'est ajouté aux VM.

Le répertoire de mesure est root:root 0755, le snapshot root:root 0644.
Le lecteur refuse un fichier ou un parent inscriptible par l'uid des PR,
un lien et un fichier spécial. Le workflow ci-disque.yml de la base appelle
le script installé et protégé, en mode publication, et publie la date
physique du snapshot ; la fin du job ne rafraîchit jamais cette date.
Une mesure forgée par un job ne peut donc pas résoudre ci_disk_low.

La collecte reste bornée à cinq minutes entre deux lectures. À partir de
**60 minutes** sans mesure récente d'un hôte attendu, le veilleur demande
une exécution du workflow dédié sur la base. Ce choix laisse encore une
part de 45 minutes en file et cinq minutes de sonde avant la borne de
fraîcheur de deux heures (60 + 45 + 5 < 120). Une exécution dédiée déjà
queued ou in_progress empêche un doublon. L'intention de dispatch est
consommée en base avant l'appel HTTP, au plus une fois par heure par dépôt,
y compris après redémarrage ou réponse ambiguë. Un échec reste visible par
l'âge de la mesure ; aucune attente ni relance en boucle n'est ajoutée.

Seul ci-disque.yml, sur la base et pour schedule ou workflow_dispatch,
alimente la table physique ci_disk_state et peut résoudre l'alerte. Les
annotations ameesh-ci-disk/1 des autres runs, PR et forks compris, sont des
signaux **uniquement levants** : une mesure saine est ignorée, une mesure
basse ouvre ou aggrave une alerte jusqu'à un relevé dédié plus récent. Les
hôtes doivent déjà appartenir au périmètre du workflow dédié. Les signaux
ne remplacent jamais une mesure physique et n'en repoussent pas l'âge.

Chaque refus de marge #5742 publie ameesh-ci-disk-refusal/1, même sous
ENOSPC : aucun fichier n'est écrit par les jobs. Le veilleur lit aussi les
annotations des jobs réussis (carte.yml peut absorber le code 75). Les
refus sont conservés dans ci_disk_refusals (migration additive 0544), avec
un identifiant dédupliqué et l'acquittement dans la transaction du courrier.
Un courrier par hôte et par passage regroupe les signaux ; ceux arrivés
ensuite restent dus au passage suivant. Aucun reason, job, runner ou autre
texte libre du code testé ne passe dans le corps ni la charge du courrier :
uniquement des identifiants validés et des nombres bornés. Ni test négatif
ni preuve de réussite ne sont fabriqués par ce canal non fiable.

Activation par le bureau après intégration : appliquer la migration 0544
et les contrats de lecture superviseur (métadonnées d'état, payload réservé
aux contenus), installer le script et les deux services/timers avec
install-disque.sh --install, lancer une première mesure protégée, vérifier
install-disque.sh --check et dispatcher le workflow dédié sur la base. Le
nom physique du snapshot doit correspondre au label matrix.host ; si
nécessaire, AMEESH_CI_DISQUE_HOST se pose dans le fichier d'environnement
root-owned du service. Vérifier la date physique reçue et les alertes sur
une mesure absente ou périmée. Aucun déploiement n'est revendiqué par la PR.

Le seuil reste **5 GiB** et **10 000 inodes**. Chaque part publie le libre
initial, le minimum observé sur / toutes les secondes et leur différence.
Cette baisse inclut les jobs concurrents : elle n'est pas une attribution
exclusive. Comparer ces observations sur les VM de 60 Go avant de modifier
le seuil ; aucune nouvelle mesure sous charge n'est revendiquée ici.

Retour arrière : réinstaller le script/service et le workflow précédents.
Retirer le timer de mesure dédié s'il n'est plus utilisé ; les tables
additives peuvent rester. La surveillance revient aux annotations du
workflow de base et conserve l'alerte de fraîcheur en cas de silence.
