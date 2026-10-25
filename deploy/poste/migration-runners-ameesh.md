# Runbook — exécuteurs de pc-smichea sous l'utilisateur `ameesh` (#3549)

Décision owner **#135872 a**, signée le 25/10/2026 à 11:41Z (approbation #110),
suite de **#133454 a** : sur pc-smichea, les exécuteurs d'agents passent sous
un utilisateur Unix distinct, `ameesh`, comme sur les VM, avec une clé dédiée
dans son home. Le `~/.ssh` (0700) de l'owner leur devient illisible, et c'est
vérifiable. Les séances GUI et les appareils restent au bureau, sous le compte
de l'owner, hors des runners. **Inverse** : revenir aux unités utilisateur de
smichea (une commande, § 10).

Ce lot est un **livrable de préparation** : le runbook et le script, sans
aucun geste d'hôte. L'exécution est un geste du bureau (§10 du canon), dans un
lot suivant ; ce lot se fige sur le runbook relu. Le script :

* `deploy/poste/migration-runners-ameesh.py` — les étapes, idempotentes,
  journalisées sous `/var/lib/ameesh/migration-runners/` (0700), sans secret
  dans le journal ;
* le présent runbook — l'ordre, les gardes, les preuves, le retour.

Le script ne pose aucun `sudo` : le bureau le lance en root. `verifier` est
une lecture seule ; le lancer depuis le compte smichea (ou root).

## 1. Périmètre

**Dans le périmètre** : les 8 unités `ameesh-runner-agent@<agent>.service` du
poste, leur home ameesh, leurs worktrees et clones sous
`/home/smichea/development/manaty/*`, l'état XDG (`~/.local/state/ameesh`,
`ameesh-host`, hooks de courrier), la configuration ameesh (`config.json`,
`env`, profils, `keys/`, `cles-ci/`), le DSN (dans `config.json`), le canon,
le venv et les comptes de harnais.

**Hors périmètre** :

* le bureau, `chat-smichea` et `veille` (sessions **externes**, pas des
  runners) : elles restent sous smichea ;
* les séances GUI et les appareils : bureau, sous le compte de l'owner ;
* `ameesh-notify`, `ameesh-credentials`, `ameesh-host-publish`,
  `ameesh-release` et les minuteries de rôle : ils restent sous smichea dans
  ce passage (conséquences § 11) ;
* la clé root de l'owner vers les VM de CI : elle reste dans `~/.ssh` de
  smichea ; les agents ne la lisent plus, et n'en reçoivent pas de copie.

## 2. Inventaire de référence (25/10/2026)

L'inventaire vivant est donné par `verifier` ; ce tableau est le contrôle de
bon sens, pas une liste à maintenir.

| agent | harnais | état au 25/10 | unité |
|---|---|---|---|
| `ameesh-dev1` | claude | actif | `ameesh-runner-agent@ameesh-dev1.service` |
| `ameesh-dev2` | claude | actif | `ameesh-runner-agent@ameesh-dev2.service` |
| `ameesh-dev4` | claude | actif | `ameesh-runner-agent@ameesh-dev4.service` |
| `ameesh-dev7` | claude | actif | `ameesh-runner-agent@ameesh-dev7.service` |
| `ameesh-cx3` | codex | actif | `ameesh-runner-agent@ameesh-cx3.service` |
| `codex2` | codex | actif | `ameesh-runner-agent@codex2.service` |
| `claude3` | claude | arrêté | `ameesh-runner-agent@claude3.service` |
| `orchestrator` | other | arrêté | `ameesh-runner-agent@orchestrator.service` |

Le poste est `laptop` pour le noyau, `pc-smichea` au registre ; les baux
portent `runner-<agent>@laptop` (`%H`), état conservé par la migration. Le
poste est Arch/Omarchy, systemd 261, AppArmor désactivé : le profil du § 12 de
`docs/EXPLOITATION.md` n'est pas nécessaire, mais le témoin root #3444 reste
en place tant que le retour arrière est possible.

Volumes mesurés : `/home/smichea/development` ~366 Gio libres,
`/home/smichea/.local/state/ameesh` ~146 Gio libres, ~34 Gio de répertoires
temporaires (dont une partie seulement est à migrer). Le script exclut les
caches régénérables (`node_modules`, `target`, `dist`, `.menage/`,
`events.jsonl`) : le besoin réel est bien inférieur au total, et `preparer`
vérifie l'espace avant de copier.

## 3. Modèle de sécurité

La séparation vient du **compte Unix**, pas d'un bac à sable contournable par
le même uid. Ce que la migration établit :

* `ameesh` n'est ni dans `docker`, ni dans `sudo`/`wheel`, ni dans `adm` : ces
  groupes donneraient un root de fait (un conteneur peut monter `/` et lire
  `/home/smichea/.ssh`). Le script **refuse** un compte cible dans l'un de ces
  groupes ; `preuve` rend la matrice rouge si le groupe `docker` apparaît ;
* `~/.ssh` de smichea (0700) n'est pas lisible par ameesh : preuve directe
  par listage en EACCES, pas une simple lecture de mode ;
* ameesh a sa **propre** clé SSH (ed25519, sans phrase, dédiée), dans son
  `~/.ssh` (0700, clé 0600), jamais copiée de l'owner ;
* les identifiants des harnais sont copiés **contrôlés** (Codex
  `auth.json`, clé DeepSeek, `gh hosts.yml`) ou ressaisis par l'owner (Claude,
  connexion web) ; aucun `cat`, aucune valeur dans le journal ou sur une ligne
  de commande ;
* `~/.config/ameesh/bascule.env` (cibles SSH **root** des VM) et
  `notify.env` (jeton ntfy) ne sont **pas** copiés dans le home ameesh ;
  `poste.sh` ne peut donc pas être lancé par ameesh tel quel (§ 11) ;
* les pièces #3444 (`cles-owner.conf`, `InaccessiblePaths=%h/.ssh`) et #3540
  (`git-agents.conf`, HTTPS + `GIT_SSH_COMMAND`) ne sont **pas** recopiées :
  le nouveau compte rend le masque inutile, et le montage qui cassait
  `/etc/ssh/ssh_config.d/20-omarchy-keepalive.conf` disparaît avec lui. Elles
  sont sauvegardées puis retirées à la bascule, restaurées au retour ;
* root n'écrit jamais à travers un lien dans `~ameesh` : `lchown`, fichiers
  temporaires `mkstemp` et `fchmod`/`fchown` par descripteur, liens refusés ;
  `preparer`/`identites` ne s'exécutent plus une fois les agents passés sous
  ameesh (témoin monotone `cible_demarree`, écrit avant le premier start
  cible et jamais effacé ; sinon un lien planté serait ameesh→root, et
  `rsync --delete` emporterait leur travail).

Ce que la migration **ne protège pas** : root (le bureau et l'owner), un
groupe privilégié ajouté plus tard, l'accès physique, et les secrets que les
agents doivent lire par nature (`config.json` contient le DSN). Le home de
l'owner n'est pas un coffre : `~/.ssh` est le périmètre de la décision, et il
est vérifié.

## 4. Prérequis

À vérifier avant de lancer quoi que ce soit :

1. **Code relu et fusionné** sur la cible, puis présent sur le poste. Le
   script s'exécute depuis un checkout du commit de fusion (il n'a pas besoin
   d'être installé dans le venv) ; l'instantané installé
   (`~/.local/share/ameesh/src-current`) est la référence pour le venv,
   jamais le worktree du lot.
2. **Bureau joignable** avec root (délégation §10). `runuser`, `loginctl`,
   `systemctl`, `python3`, `rsync`, `git`, `ssh`, `gh` disponibles.
3. **Réseau** : `pip` (installation du venv), GitHub (SSH et `gh`). Le poste
   est sur secteur ; batterie > 50 %.
4. **Créneau calme** : hors coupures de 11:00/23:00 Europe/Paris (marge
   ±60/30 min), hors paquet en cours sur `main`, hors drill de gouvernance.
   `verifier` le contrôle ; `basculer` refuse sans ces trois feux verts.
5. **Gel de chantier** : les six agents actifs poussent leur travail et
   n'ouvrent pas de nouveau lot. Les arrêtés restent arrêtés.
6. **Owner disponible pour une seule séance** : les connexions web Claude
   (une par compte déclaré) et l'ajout de la clé publique sur GitHub. Le
   bureau conduit la séance, l'owner ne saisit que dans son navigateur.
7. **Harnais installés pour ameesh** à la **même version** que smichea :
   `claude`, `codex`, `gh` (et `dsh`/`AMEESH_DSH_BIN` si un compte DeepSeek
   est déclaré). Le script les vérifie et refuse une version différente.
   Prévoir aussi la **chaîne d'outils des dépôts** (Node/npm, Python,
   `mise` si le compte source l'utilise) : les tests des lots s'exécutent
   avec le PATH de l'unité (`~/.local/share/mise/shims` puis
   `~/.local/bin` puis le système). Une version différente peut faire
   échouer les tests ; `verifier` signale au minimum les binaires absents,
   et `doctor --harness` les harnais.
8. **Canon** : corriger d'abord, dans la fiche `pc-smichea`, les chemins des
   sessions externes (`bureau`, `chat-smichea`) en absolu `/home/smichea/...`
   plutôt que `~/...` : `canon sync` lancé sous ameesh développerait `~` dans
   le home ameesh. À défaut, le cwd de `bureau` devient `/home/ameesh/Work`
   (sans effet sur une session externe, mais trompeur) ; le script le signale.
9. **Plan de sortie** : fenêtre de 2 h (drain maximal 36 min par unité, puis
   copie finale et démarrages), et le bureau sait faire `retour` (§ 10).
10. **Capacité** : `/home/smichea/development` a ~366 Gio libres (volume de
   développement), mais `/home/ameesh` est sur le volume d'état (~146 Gio
   libres). Une copie intégrale de `development/manaty` peut donc ne pas
   tenir. `preparer` et `verifier --avant-bascule` mesurent la source et ce
   que la cible porte déjà (`du -sb`, caches et `events.jsonl` exclus),
   **agrègent par système de fichiers réel** (`st_dev` : `development` et
   `home` peuvent partager le même volume) et refusent sous 20 % de marge,
   au moins 5 Gio — même quand le supplément calculé est nul, `rsync` a
   besoin de temporaires. `preparer` refuse **avant la première copie**.
   Deux voies, à choisir avant le geste :
    * **recréer** les clones sous `/home/ameesh/development/manaty` depuis
      l'origine (mêmes branches/commits) et n'y copier que les worktrees
      actifs et les modifications non poussées — la voie du lot, « migrés ou
      recréés » ; exige un inventaire cwd/branche des agents actifs. **Voie
      manuelle** : `preparer_repos` (pré-copie puis copie finale) fait un
      `rsync --delete` de la source et écraserait ces clones ; ce mode n'est
      pas automatisé par ce script (limite P2 connue, lot séparé). Pour un
      premier passage, préférer le volume dédié ci-dessous ;
    * **placer** `/home/ameesh/development` sur le volume de développement
      (bind mount persistant, par exemple
      `/home/smichea/development/.ameesh` → `/home/ameesh/development`, monté
      par une unité `.mount` ou `/etc/fstab`), puis copier normalement ; geste
      root du bureau, à tracer.
   Sans l'une de ces voies, le script s'arrête avant tout arrêt de service.
11. **Lancer hors session liée** : le bureau est une session **liée** sur
   pc-smichea (`ameesh mail bind --pid`). `ameesh pause`/`resume` refuse tout
   descendant d'une session liée (`by_ancestry`, sans filtre uid), même via
   `sudo`/`runuser` : `basculer` et `retour` échoueraient aux pauses. Lancer
   ces deux étapes dans une **unité transitoire**, qui casse l'ascendance :

   ```sh
   sudo systemd-run --collect --wait --pipe --unit migration-3549-basculer \
     --working-directory "$PWD" \
     python3 deploy/poste/migration-runners-ameesh.py basculer
   ```

   `preparer`, `identites`, `verifier` et `preuve` peuvent rester en direct.
   Le script détecte la session liée et refuse **avant tout arrêt** avec la
   commande exacte ; un `retour --force` se lance de la même façon. Suivre
   `journalctl -u migration-3549-basculer` si besoin.

## 5. Étape 1 — vérifier (lecture seule)

```sh
sudo deploy/poste/migration-runners-ameesh.py verifier
sudo deploy/poste/migration-runners-ameesh.py verifier --json   # pour un journal
```

Le script affiche : hôte configuré, comptes source/cible, mode de `~/.ssh`,
version installée, canon, agents execute et état de leurs unités, `linger`,
créneau, file des paquets, drill. Il ne modifie rien.

Puis, avant la bascule, la garde stricte :

```sh
sudo deploy/poste/migration-runners-ameesh.py verifier --avant-bascule
```

Elle ajoute : compte cible hors groupes privilégiés, linger, venv et unités
cibles, mêmes versions de harnais, auth Codex/DeepSeek, logins Claude
présents, clé GitHub qui répond, espace disque, `~/.ssh` source privé. Un
seul manque bloque `basculer`.

## 6. Étape 2 — préparer (aucun arrêt de service)

```sh
sudo deploy/poste/migration-runners-ameesh.py preparer
```

Le script, dans cet ordre :

1. crée `ameesh` (`useradd --create-home --home-dir /home/ameesh --shell
   /bin/bash --user-group`), sans mot de passe, sans groupe privilégié ;
2. crée les dossiers (home 0750, `.ssh` 0700, `.config/ameesh` 0700,
   `.local/share/ameesh`, `development/manaty`, `canon`) et pose
   `loginctl enable-linger ameesh` (gestionnaire utilisateur au boot) ;
   **avant la première copie**, vérifie l'espace libre (source moins ce que
   la cible porte déjà, `du -sb`, **agrégé par système de fichiers réel**,
   marge de 20 % ou 5 Gio) et refuse si `/home` ou le volume de
   développement est trop plein (§ 4.10) ;
3. copie `config.json` (0600, chemins réécrits), `env` (0600), `.pgpass`,
   `runner-profiles/`, `keys/`, `cles-ci/` ; **pas** `bascule.env`, **pas**
   `notify.env`, **pas** `git-agents.conf` ;
4. copie l'état XDG utile (sessions, marqueurs de worktrees, radar/trains,
   journaux de mise à jour) sans les caches, les verrous et les
   `events.jsonl` ; réécrit les marqueurs `worktree.json` ;
5. copie le canon (git) et vérifie sa tête ;
6. pré-copie `development/manaty` (clones et worktrees), réécrit les fichiers
   `.git` des worktrees liés, répare les liens par `git worktree repair` ;
7. reconstruit le **venv** pour le nouveau home, depuis l'instantané installé
   (jamais une copie du venv : ses shebangs portent l'ancien home) :
   `python3 -m venv`, puis `bascule.py preparer`/`activer` ; vérifie
   `ameesh --version` ;
8. installe les unités et drop-ins du runner dans
   `/home/ameesh/.config/systemd/user/`, réécrit `/home/smichea` →
   `/home/ameesh`, **exclut** `cles-owner.conf` et `git-agents.conf`,
   `daemon-reload` — **sans activer ni démarrer** aucune unité cible. Tant
   que la source tourne, une activation cible ferait deux runners après un
   redémarrage ; l'échange des états `enabled` se fait dans `basculer`, source
   arrêtée.

Aucun exécuteur n'est arrêté. En cas d'échec, la source continue ; corriger et
relancer `preparer` (idempotent). `preparer` et `identites` n'acceptent que
les états `neuf`/`prepare` : après une bascule ou un `retour`, le home cible
a pu être utilisé par les agents (liens plantés, travail non poussé) et le
script **refuse** d'y écrire en root ; `rsync --delete` ne doit pas non plus
emporter le travail créé sous ameesh. Le refus est porté par un **témoin
monotone** `cible_demarree`, écrit dans `etat.json` juste avant la première
tentative de démarrage cible et jamais effacé : même si le canari a tourné
puis est sorti ou failed (donc plus aucune unité active), l'état ne revient
pas à `prepare` et `preparer`/`identites` restent refusés. Une nouvelle
tentative exige de recréer le home cible (geste dédié), pas un simple
`preparer`.

## 7. Étape 3 — identités (une seule séance owner)

```sh
sudo deploy/poste/migration-runners-ameesh.py identites
```

Le premier passage **échoue volontairement** et affiche ce qui manque : il
prépare la clé SSH et les dossiers, copie Codex/DeepSeek/`gh`, puis liste les
connexions Claude à faire. Les dossiers d'identités (`~/.config/gh`,
`~/.codex`, `.claude*`, config_dir secondaires) sont créés en 0700 **et
donnés au compte ameesh** avant tout appel sous son compte : un dossier
root 0700 barriérerait le harnais. C'est la séance owner :

### 7.1 Clé SSH dédiée

Le script génère `~ameesh/.ssh/id_ed25519` (0600) et affiche la clé publique
et son empreinte. Le bureau l'ajoute à GitHub, avec le compte de l'owner
(geste GitHub, pas root) :

```sh
gh ssh-key add /home/ameesh/.ssh/id_ed25519.pub \
  --title "pc-smichea ameesh runners (#3549)"
```

Puis vérifier : `ssh -T git@github.com` doit répondre « successfully
authenticated » **sous ameesh**. Le script contrôle aussi l'empreinte de la
clé d'hôte GitHub (`SHA256:+DiY3wvvV6TuJJhbpZisF/zLDA0zPMSvHdkr4UvCOqU`).

### 7.2 GitHub, Codex, DeepSeek par copie contrôlée

* `gh` : copie de `~/.config/gh/hosts.yml` (0600), puis
  `gh auth setup-git` sous ameesh — l'assistant pointe le `gh` réel du PATH ;
  `/usr/bin/gh` est le chemin attendu sur le poste ;
* Codex : copie de `auth.json` et `config.toml` de chaque `config_dir`
  déclaré (0600, dossier 0700) ;
* DeepSeek : la clé reste dans `env` (0600) ou dans `keys/` ; le script
  vérifie la variable/fichier de chaque compte déclaré, sans l'afficher.

### 7.3 Claude — la séance web groupée

Le script imprime une commande par compte (`primaire`, `secondaire`, …) :

```sh
sudo -u ameesh -H env CLAUDE_CONFIG_DIR=/home/ameesh/.claude \
  claude auth login
sudo -u ameesh -H env CLAUDE_CONFIG_DIR=/home/ameesh/.claude-secondary \
  claude auth login
```

L'owner ouvre le navigateur, se connecte au **bon compte** et approuve ; le
bureau ne saisit ni mot de passe ni code. Une seule séance pour tous les
comptes. Si le poste a une session graphique, passer aussi `DISPLAY` et
`WAYLAND_DISPLAY` permet l'ouverture automatique ; sinon la commande affiche
l'URL à ouvrir.

Puis relancer `identites` : il doit finir par « identités prêtes ». Le script
vérifie `claude auth status` par dossier (`loggedIn: true`), `ssh -T`,
`gh auth status`, et les versions de harnais **identiques** à la source.

## 8. Étape 4 — basculer (drain par vagues, hors créneau)

Le bureau est une session liée : lancer **sous unité transitoire** (§ 4.11),
sinon le script refuse avant tout arrêt.

```sh
sudo systemd-run --collect --wait --pipe --unit migration-3549-basculer \
  --working-directory "$PWD" \
  python3 deploy/poste/migration-runners-ameesh.py basculer
# options : --canari <agent> (défaut codex2), --attente-arret <secondes> (2200)
```

1. **Gardes** : `verifier_avant_bascule` (créneau, paquet, drill, comptes,
   auth, versions, espace agrégé par système de fichiers) ; refus avant tout
   arrêt.
2. **Pauses opérateur** : `ameesh pause <agent> --reason "migration #3549 …
   <horodatage>" --not-after <+2 h>` pour les six actifs. `--not-after` borne
   la **pose** à deux heures (échéance comparée à l'horloge de la base), pas
   la durée de la pause : c'est `resume` qui la lève. L'admission est fermée ;
   un tour en cours n'est pas interrompu. Une pause étrangère est conservée et
   ne sera pas levée. Une pose partielle est journalisée dans `etat.json` et
   levée par l'annulation ou le retour.
3. **Vague 1 — arrêt** : `systemctl --user stop --no-block` sur les six
   unités de smichea, en parallèle : la commande met les arrêts en file et
   rend la main, elle n'attend pas un tour. Chaque SIGTERM draine son tour,
   borné par `TimeoutStopSec=35min` ; le script attend `ActiveState=inactive`
   **et** `MainPID=0` pour chacune, au plus `--attente-arret` (2200 s). Un
   arrêt non prouvé interdit tout démarrage cible et remet la source en
   service. Une fois l'arrêt prouvé, les unités **source encore activées au
   boot** sont désactivées : un redémarrage ne doit pas relancer les deux
   jeux.
4. **Copie finale** : état et `development/manaty` recopiés source
   quiescente, `.git` réparés ; `canon sync --host pc-smichea` sous ameesh
   pour que les `cwd` des agents passent sous `/home/ameesh/...` ; vérification
   que chaque `cwd` existe. Si le canon n'a pas été corrigé (§ 4.8), le cwd de
   `bureau` change aussi : le signaler, ce n'est pas un runner.
5. **Vague 2 — canari** : démarrage de `codex2` (ou `--canari`) sous ameesh ;
   attente d'un bail vivant (`runner-codex2@laptop`), puis levée de sa pause
   et **courrier test** : envoi depuis `bureau`, attente d'un tour terminé
   (max 7 min). C'est la preuve de la chaîne entière (DB, cwd, harnais, mail,
   git/gh pour Codex).
6. **Vague 3 — le reste** : démarrage des cinq autres actifs, un par un,
   attente du bail pour chacun. Les deux arrêtés (`claude3`, `orchestrator`)
   ne sont pas démarrés : leurs unités sont transférées et restent inactives.
7. **Levée des pauses** de la migration, avec le motif exact ; un échec de
   levée est signalé et laissé au bureau (jamais de levée d'une pause
   étrangère). Puis les unités cible actives sont **activées au boot** : à
   partir de là, un redémarrage ne démarre que le jeu ameesh. Pendant la
   fenêtre de bascule (source désactivée, cible pas encore activée), ne pas
   redémarrer le poste.
8. **Retrait des pièces devenues inutiles** : `cles-owner.conf` (#3444) et
   `git-agents.conf` (#3540) sont sauvegardés sous
   `/var/lib/ameesh/migration-runners/sauvegarde/`, puis retirés de la source
   et `daemon-reload`. Le témoin root et le profil AppArmor restent en place
   pour que `retour` puisse les restaurer.

Si la bascule échoue **avant toute tentative de démarrage cible** (témoin
`cible_demarree` absent), le script remet la source en service et lève ses
pauses ; avant de la redémarrer, il rétablit les `cwd` sous `/home/smichea`
(`canon sync`) et le vérifie. Si ce rétablissement échoue, il s'arrête
**sans** redémarrer la source et le dit. Si elle échoue **après une
tentative de démarrage cible** — même si le canari a tourné puis est sorti ou
failed et que toutes les unités cible sont désormais inactives — le témoin
interdit l'annulation : les unités cible restent en place, les pauses non
levées gardent l'admission fermée, **aucun retour à `prepare`**, et le
message dit de lancer `retour`.

**Interruption** (Ctrl-C, `OSError`, coupure) dans la fenêtre critique :
- **avant** le premier `systemctl start` cible (témoin absent) : le script
  remet la source en service et lève les pauses de lui-même ; l'ensemble
  actif est écrit dans `etat.json` **avant le premier arrêt**, donc après une
  coupure brutale (SIGKILL, redémarrage) `retour --force` le relit et
  redémarre exactement ces unités (§ 10) ;
- **après** (témoin présent, même si aucune unité cible n'est active) : pas
  d'annulation, `retour` comme ci-dessus.

Ne pas relancer `preparer` dans ces états : le témoin le refuse, précisément
pour ne pas réécrire en root dans un home que les agents ont utilisé.

## 9. Étape 5 — preuve

```sh
sudo deploy/poste/migration-runners-ameesh.py preuve
```

La matrice est écrite sous `/var/lib/ameesh/migration-runners/preuve.json`.
Elle est verte seulement si :

| preuve | commande sous ameesh | attendu |
|---|---|---|
| `~/.ssh` de l'owner | `python3 -c 'os.listdir("/home/smichea/.ssh")'` | `EACCES` |
| pas de root de fait | `id -Gn` | ni `docker`, ni `sudo`, ni `wheel` |
| clé dédiée | `ssh -T git@github.com` | « successfully authenticated » |
| git en SSH | `git -C <clone> fetch git@github.com:manaty/ameesh.git refs/heads/main` | code 0 |
| `gh` | `gh auth status` | compte connecté |
| doctor | `ameesh doctor` | `verdict    : OK` |
| harnais | `ameesh doctor --harness` | résolus |
| cwd | `ameesh show <agent> --json` | sous `/home/ameesh/` et existant |
| baux | `ameesh show <agent> --json` | `lease_owner=runner-<agent>@laptop`, non expiré |
| comptes | `ameesh accounts list --json` | aucun `problems` d'identifiants |
| unités | `systemctl --user list-units --state=failed` | aucune en échec ; actifs = ensemble d'avant |
| courrier | journal de `basculer` | `courrier test livré` pour le canari |
| clé | `.pub` source ≠ `.pub` cible | clé dédiée distincte |

La preuve ne remplace pas la relecture : un KO est un **lot correctif** ou un
`retour`, pas un « à suivre ». Le canari a consommé un vrai tour ; son clone
et ses worktrees sont ceux d'ameesh, la source n'est plus alimentée.

## 10. Retour arrière — une commande

Comme la bascule, le retour pose des pauses : il se lance **sous unité
transitoire** (§ 4.11).

```sh
sudo systemd-run --collect --wait --pipe --unit migration-3549-retour \
  --working-directory "$PWD" \
  python3 deploy/poste/migration-runners-ameesh.py retour
# --force : même si des tours ont eu lieu sous ameesh depuis la bascule,
#           ou après une bascule interrompue (état resté `prepare`)
```

Le script :

1. refuse si un agent (hors canari) a travaillé sous ameesh depuis
   `migre_le` — un retour perdrait ce travail ; `--force` est explicite ;
2. pose des pauses de retour, arrête les unités ameesh
   (`stop --no-block`, arrêt prouvé et borné), sans démarrer la source
   avant ;
3. restaure `cles-owner.conf` et `git-agents.conf` depuis la sauvegarde,
   `daemon-reload` sous smichea ;
4. `canon sync` sous smichea pour ramener les `cwd` sous `/home/smichea`,
   **désactive au boot** les unités ameesh et **vérifie** que la
   désactivation a pris (`UnitFileState`) avant de **réactiver** celles de
   smichea — un `disable` refusé arrête le retour sans redémarrer la source,
   pour ne pas laisser les deux jeux activés au redémarrage ; puis redémarre
   **le même ensemble** d'unités actives qu'avant (les arrêtées restent
   arrêtées) ; cet ensemble est celui écrit dans `etat.json` avant le
   premier arrêt, donc `retour --force` répare aussi une bascule interrompue
   avant `stage=bascule` ;
5. lève ses pauses — y compris celles restées posées par une bascule
   interrompue — vérifie les unités actives, écrit `stage=retour`.

Limites : le retour ne recopie pas les données créées sous ameesh (worktrees,
commits non poussés) ; il remet les **services** et les cwd. Après un tour de
canari, `--force` suffit car le canari est exclu du contrôle ; après du vrai
travail, pousser ou copier d'abord, puis `retour`, ou corriger en avant. Le
compte ameesh, son home et ses données restent en place, mais une **nouvelle
tentative** ne repasse pas `preparer` par-dessus : le script refuse d'écrire
en root dans un home que les agents ont utilisé (§ 6). Recréer le home cible
est un geste dédié, tracé ; sinon, le retour est définitif.

## 11. Après la migration — ce qui reste à faire

La migration est un **geste de préparation** ; elle ne rend pas le poste
« actif » à elle seule.

1. **Mises à jour du poste** : `poste.sh` tourne aujourd'hui sous smichea et
   pilote `systemctl --user` de smichea. Après la bascule, un tick de release
   qui l'appelle met à jour le venv de smichea et ne redémarre **aucun**
   runner ameesh. Il faut, dans un lot suivant :
   * soit un mode `poste.sh` qui cible le gestionnaire d'ameesh (via
     `runuser`/`machinectl`, sans donner à ameesh le `bascule.env` root) ;
   * soit déplacer les services d'exploitation du poste sous ameesh, avec un
     accès serveur moindre (clé dédiée, pas la root de l'owner).
   Jusque-là, **ne pas compter sur le staging automatique pour le poste** :
   les runners ameesh restent sur le code installé par cette migration, et
   toute mise à jour est un geste manuel du bureau, tracé au lot.
2. **`ameesh-notify`** : #3484 prévoit son arrêt et son déplacement ; il reste
   sous smichea en attendant, sans effet sur les runners.
3. **Canon** : `canon sync` sur pc-smichea doit désormais être lancé sous
   ameesh (ou depuis un autre hôte ameesh) pour développer `~` dans le bon
   home ; corriger la fiche pour les sessions externes (§ 4.8).
4. **Docker** : aucun runner n'a le groupe `docker` ; si un agent a besoin de
   conteneurs, installer un Docker **rootless** sous ameesh (socket dans
   `/run/user/<uid>`), jamais le groupe `docker` du poste. À refaire dans un
   lot dédié, avec la même preuve : `docker run -v /home/smichea/.ssh` doit
   échouer.
5. **Sessions** : les stores `.claude`/`.codex` ne sont pas copiés ; le
   runner constate l'identifiant inconnu (`_session_connue`), écarte la
   session et en ouvre une neuve avec le brief de reprise. Les lots, le
   courrier et les worktrees sont dans la base et dans les clones : rien de
   fonctionnel n'est perdu, le contexte de conversation l'est. Le noter dans
   le courrier de fin aux agents.
6. **Nettoyage** après la fenêtre de retour (7 jours) : retirer ou masquer les
   unités de smichea devenues inutiles, puis, si plus aucun `retour` n'est
   possible, le témoin root `/usr/local/libexec/ameesh/verifie-cles-owner` et
   le profil AppArmor (facultatif ; ils n'ouvrent que userns). Ne pas les
   retirer avant : `retour` en a besoin pour redémarrer la source.

## 12. Dépannage

| symptôme | cause probable | geste |
|---|---|---|
| `preparer` : `useradd` refuse | compte existant, home inattendu, groupe privilégié | `id ameesh`, `gpasswd -d ameesh docker`, corriger le home à la main puis relancer |
| `preparer` : `linger non posé` | `systemd-logind` ou `/run/user/<uid>` absent | `loginctl enable-linger ameesh`, `loginctl show-user ameesh -p Linger`, relancer |
| `preparer` : `bascule.py … a échoué` | pip/réseau, dépendance, disque | lire la sortie ; la source n'a pas bougé ; corriger et relancer |
| `preparer` : `espace insuffisant` | le total sur un même système de fichiers dépasse le libre (marge rsync 5 Gio incluse) | libérer de l'espace ou monter le volume dédié (§ 4.10) ; la source n'a pas bougé |
| `preparer`/`identites` : `état 'retour' …` | le home cible a été utilisé par les agents (liens, travail ameesh) | ne pas forcer : recréer le home cible (geste dédié) ; `rsync --delete` emporterait le travail ameesh |
| `preparer` : gabarit sans `SSH_AUTH_SOCK` | unité source antérieure à #3444 | réinstaller `deploy/systemd/ameesh-runner-agent@.service` sous smichea, `daemon-reload` (le gabarit seul ne redémarre pas les runners), relancer |
| `identites` : `claude … loggedIn false` | login web non fait, mauvais dossier | refaire la commande du § 7.3 pour **ce** dossier |
| `identites` : `ssh GitHub` échoue | clé publique non ajoutée, `known_hosts` | `gh ssh-key add`, vérifier l'empreinte, relancer |
| `basculer` : `arrêt non prouvé` | tour long, unité bloquée | attendre la borne ; ne jamais démarrer la cible ; `journalctl --user -u …` sous smichea |
| `basculer` : canari sans bail | auth, cwd, harnais, DB | lire le journal de l'unité sous ameesh ; corriger ; `retour` si la source a été arrêtée |
| `preuve` : `doctor` rouge | config, DB, unité | ne pas retirer les sauvegardes ; `retour` ou correctif |
| `retour` : unité source inactive | drop-in/witness retiré, `daemon-reload` | le script s'arrête ; restaurer le témoin, relancer `retour` |
| `retour` : pause non levée | deux CLI en échec | la commande exacte est imprimée : `ameesh resume <agent> --operator --reason … --expect-reason …` |
| `basculer`/`retour` : `session liée détectée` | le bureau est lié (pause/resume refuse un descendant) | relancer la commande sous `systemd-run` comme l'imprime le script (§ 4.11) |
| redémarrage pendant la bascule | source déjà désactivée, cible pas encore activée | `retour` (état `bascule`) : il réactive la source et redémarre son ensemble actif |
| `basculer` interrompu (Ctrl-C, coupure) **avant** le premier start cible | témoin `cible_demarree` absent, actifs écrits avant l'arrêt | `retour --force` : il redémarre l'ensemble actif de `etat.json` et lève les pauses ; ne pas relancer `preparer` |
| canari démarré puis sorti/failed avant la fin de `basculer` | témoin `cible_demarree` présent, aucune unité cible active | ne pas annuler ni relancer `preparer` (refusé) : `retour` pour remettre la source en service |

Toujours : lire le journal, ne pas éditer `etat.json` à la main, ne pas lancer
deux gestes en parallèle (le verrou le refuse).

## 13. Traces et clôture

* journal JSONL : `/var/lib/ameesh/migration-runners/journal.jsonl` (racine
  0700) — étapes, horodatages, codes, jamais un secret ;
* état : `etat.json` (stage, agents, actifs, pauses, pièces retirées, témoin
  monotone `cible_demarree`) ;
* preuves : `preuve.json` (matrice) ;
* sauvegarde : `sauvegarde/` (drop-ins #3444/#3540, 0600) ; à conserver
  jusqu'à la fin de la fenêtre de retour ;
* note de lot d'exécution : les liens vers le journal, `preuve.json`, les
  commandes de vérification et les écarts (canon externe, services
  d'exploitation, Docker rootless, sessions neuves).

Le lot #3549, lui, se clôt sur la **relecture du runbook** et du script ; son
« fait » est la préparation relue, pas la migration exécutée. L'exécution est
un lot du bureau ; « actif » au sens du canon §5 suppose les points du § 11
traités, en particulier la mise à jour du poste.
