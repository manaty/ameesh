# Comptes multiples par fournisseur (L30, L74, L117)

Les comptes au forfait d'un fournisseur forment un **réservoir**, pas une
liste de secours (décision
[0034](design/decisions/0034-consommer-d-abord-ce-qui-expire.md), qui amende
[0027](design/decisions/0027-bascule-automatique-entre-comptes.md)) : un
forfait non consommé avant sa remise à zéro est perdu. Avant chaque tour, le
compte est choisi ainsi :

1. **Forçage** (`ameesh accounts use`) : ce compte, ou pause s'il est au
   seuil. `ameesh accounts auto` rend la main à la règle.
2. **Continuité** : une session en cours reste sur son compte tant que
   celui-ci est sous son seuil — pas de changement en cours de session pour un
   gain marginal.
3. Sinon (ouverture de session, rotation, ou compte de la session au seuil) :
   parmi les comptes **utilisables** (profil valide, sous le seuil de la garde
   `min(90 %, part écoulée + 10 points)` dans **toutes** leurs fenêtres,
   décision 0019), celui dont le **budget s'évapore le plus vite** : la part
   inutilisée (`1 − utilisé`) de la fenêtre qui se remet à zéro le plus tôt,
   rapportée au temps qui reste (`lost_rate`, #1789/N1) — consommer d'abord ce
   qui sera perdu. À égalité, celui qui a **le plus de retard sur son rythme** :
   le plus petit rapport `utilisé / rythme` (le rythme est le plafond de la
   garde à cet instant de la fenêtre, colonne « rythme » de `ameesh accounts
   list`), pris sur sa fenêtre la plus contraignante (5 h ou 7 jours). Un compte
   sans fenêtre ouverte ou sans relevé compte pour 0 % utilisé : il passe **en
   premier**. Un **relevé partiel** (#1561 : une fenêtre datée remise à zéro
   alors qu'une autre court encore, ou un relevé non daté plus vieux que sa
   plus courte fenêtre) n'est plus un instantané courant : le compte est **non
   vérifié**, sa raison le dit, et il passe après tout compte à relevé courant,
   quel que soit son rythme lu — il reste **essayé** faute de mieux (L71,
   « essayé, pas écarté »). #1566 : ce partiel reste **informateur** — date de
   lecture connue et sous ce qui reste d'**au moins une** fenêtre datée encore
   ouverte, et **chaque** fenêtre ouverte (non datée comprise) sous son plafond
   effectif moins `RESERVE` — et garde alors le rang d'un relevé **courant**
   (jamais celui d'un relevé absent) ; sans preuve, il reste après eux. #1672
   **borne cet essai** : quand une fenêtre datée
   s'est remise à zéro, un relevé plus vieux que ce qui reste de **chaque**
   fenêtre encore ouverte est une information trop ancienne pour engager un
   tour — prudence, pas preuve d'un quota atteint —, le compte est écarté du
   placement et, sans compte plaçable, le choix pause ; la remise à zéro qui
   motive la pause le rouvre (L71, 0 %). La continuité (règle 2) garde son
   chemin : une session déjà sur le compte y reste, et c'est son tour qui
   rapporte la jauge ; un forçage manuel (règle 1) reste souverain. À égalité de
   perte et de retard, celui dont la capacité inutilisée expire le plus tôt,
   puis la plus grosse part perdue, puis l'ordre déclaré (amendement du
   2026-10-10 à 0034, lot L117 : un compte jamais utilisé ne doit pas rester
   inutilisé ; avant, le primaire, toujours en fenêtre ouverte, gagnait
   toujours).
4. Tous les comptes au seuil, ou écartés faute d'un relevé assez récent pour
   placer : pause — la raison dit lequel des deux cas s'applique.

Un relevé dont la fenêtre est échue (`resets_at` passé) compte pour **0 %**
(même règle que L71) : un compte resté à 93 % sur une fenêtre close n'est plus
écarté, il est essayé, et son premier tour rapporte sa jauge. Un relevé
**partiel** — une fenêtre datée remise à zéro depuis, une autre encore ouverte —
n'est pas dans ce cas : la fenêtre ouverte n'est qu'une **borne inférieure**
(le compte a pu servir depuis, ici ou ailleurs), donc le relevé n'est plus
courant et le compte devient **non vérifié** (#1561) : sa raison le dit au lieu
d'annoncer la valeur périmée comme courante, il n'annonce ni perte ni échéance,
et il passe après tout relevé courant, **essayé** faute de mieux (L71). #1566 :
quand il reste **informateur** (âge sous ce qui reste d'une fenêtre datée
ouverte, chaque fenêtre ouverte sous son plafond effectif moins `RESERVE`), il
garde le rang d'un relevé courant — la raison dit « partiel », pas une mesure
fraîche. #1672
borne cet essai : un relevé plus vieux que ce qui reste de chaque fenêtre
encore ouverte est trop ancien pour engager un tour, le compte est écarté du
placement et le choix pause jusqu'à la remise à zéro qui la motive. Le cas
réel du 15/10/2026 : une jauge 7 j lue à 30 % (relevé vieux de quatre jours)
alors qu'elle valait 100 %, la fenêtre de 5 h s'étant remise à zéro entre-temps
— le choix automatique y a envoyé un tour, refusé par la limite hebdomadaire ;
désormais il pause, la remise à zéro rouvrant le compte, et la continuité comme
le forçage restent les chemins ouverts vers un relevé périmé. Les jauges sont
relues à chaque choix. Jamais au milieu d'un tour. Étude :
[docs/design/etudes/comptes-multiples.md](design/etudes/comptes-multiples.md).

Le respect des conditions d'utilisation de chaque fournisseur pour l'usage de
plusieurs comptes reste de la responsabilité de l'équipe.

## 1. Préparer un compte secondaire (gestes humains)

ameesh ne se connecte jamais à un compte et ne lit jamais un identifiant :
la connexion est faite une fois, par un humain, dans un dossier dédié.

**Claude Code**

```sh
install -d -m 700 <dossier-claude-2>
# continuité des sessions : partager le stockage des sessions du primaire
ln -s <dossier-du-primaire>/projects <dossier-claude-2>/projects
# mêmes hooks et réglages que le primaire (agent-mail)
ln -s <dossier-du-primaire>/settings.json <dossier-claude-2>/settings.json
CLAUDE_CONFIG_DIR=<dossier-claude-2> claude     # puis /login, avec le compte secondaire
```

Le dossier du primaire est `~/.claude` s'il n'a pas de dossier dédié. Sans le
lien `projects`, chaque bascule fait une rotation avec résumé.

**Codex**

```sh
install -d -m 700 <dossier-codex-2>
cp <CODEX_HOME du primaire>/config.toml <dossier-codex-2>/   # et hooks.json si utilisé
CODEX_HOME=<dossier-codex-2> codex login
```

Ne partagez pas `sessions/` entre deux comptes Codex : les sessions sont
rattachées au compte qui les a créées et portent ses jauges. La bascule se
fait par rotation avec résumé.

**DeepSeek Harness (clé d'API)** : une clé par compte, soit dans une
variable de l'environnement de l'exécuteur (unité systemd, `EnvironmentFile`
0600), soit dans un fichier :

```sh
install -m 600 /dev/null <dossier-des-cles>/deepseek-b.key   # puis y coller la clé
```

## 2. Déclarer les comptes de l'hôte

Dans la configuration de l'**hôte** (`~/.config/ameesh/config.json`), jamais
dans le canon :

```json
{
  "accounts": {
    "claude": [
      {"name": "primaire"},
      {"name": "secondaire", "type": "config_dir", "path": "<dossier-claude-2>"}
    ],
    "codex": [
      {"name": "primaire"},
      {"name": "secondaire", "type": "config_dir", "path": "<dossier-codex-2>"}
    ],
    "deepseek": [
      {"name": "cle-a", "type": "api_key_env", "key_env": "DEEPSEEK_API_KEY_A",
       "hourly_usd": 5, "min_balance": 2},
      {"name": "cle-b", "type": "api_key_env", "key_file": "<dossier-des-cles>/deepseek-b.key"}
    ]
  }
}
```

* L'ordre départage les égalités de budget — perte par seconde, retard,
  échéance, part perdue (par exemple deux comptes jamais utilisés, ou les clés
  d'API sans jauge) —, et le premier sert de compte actif par défaut
  (`account_active`) tant qu'aucun choix n'est écrit.
* `config_dir` sans `path` : le **dossier par défaut du harnais** (`~/.claude`,
  `~/.codex`, `~/.dsh`), jamais celui que l'environnement de l'exécuteur
  désigne (`CLAUDE_CONFIG_DIR` / `CODEX_HOME` hérités) : un compte déclaré
  n'hérite pas du compte ambiant (L1078). Ses jauges Codex sont lues dans
  `<dossier>/sessions` ; le primaire des relevés Claude — le seul compte à qui
  appartiennent les flux sans marqueur, sessions d'avant L30 — est le profil
  dont le dossier est le dossier par défaut du harnais, jamais le premier de
  la liste : une liste réduite au seul secondaire ne lui prend pas ses relevés.
* Contrôles : dossier à l'utilisateur, **0700**, identifiants présents
  (`.credentials.json` pour Claude, `auth.json` pour Codex : présence
  seulement) ; fichier de clé **0600** ; variable de clé présente. Un compte
  qui échoue n'est jamais choisi, et `ameesh accounts list` dit pourquoi.
  Sur macOS (L65), Claude Code range ses identifiants OAuth dans le
  **trousseau** : la présence y est vérifiée par
  `security find-generic-password` (sans `-w`, jamais le secret), sur
  l'entrée globale `Claude Code-credentials` d'un compte par défaut ou sur
  l'entrée namespacée `Claude Code-credentials-<sha256(dossier)[:8]>` d'un
  compte déclaré avec `path` (Claude Code 2.1.56 et suivants) ; le fichier
  `.credentials.json` reste une preuve quand il existe. Codex garde
  `auth.json` sur macOS.
* `hourly_usd` : plafond horaire du compte (dépense des tours attribués au
  compte). Il est propre à l'hôte et s'ajoute aux plafonds du mesh, qui se
  règlent en base pour toutes les machines par `ameesh budget set` (L70,
  voir [EXPLOITATION.md](EXPLOITATION.md#plafonds-de-budget-du-mesh--ameesh-budget-l70)) ;
  un compte au seuil bascule, un plafond du mesh atteint met en pause. `min_balance` : le compte est au seuil quand son dernier solde
  relevé est au plus ce montant (relevé par l'exécuteur, ou
  `ameesh cost balance --record`).
* `adopts_unnamed: true` (facultatif, `api_key_env`, #4944) : déclare que
  **tous les relevés de solde sans compte de ce fournisseur** appartiennent à
  ce profil. Une seule adoption est permise par fournisseur ; sa valeur est
  un booléen, faux par défaut. Vérifier d'abord l'identité du compte des
  collecteurs concernés : des soldes semblables ne suffisent pas. Les vues
  de dépenses, `budget_drift` et `balance_low` fusionnent alors les lectures
  nommées et sans compte dans l'ordre chronologique, sans réécrire la base.
  Les autres comptes nommés restent des séries indépendantes, additionnées.
  Exemple de profil :

  ```json
  {"name": "compte-a", "type": "api_key_env", "key_env": "DEEPSEEK_API_KEY",
   "adopts_unnamed": true}
  ```

  Déployer d'abord le lecteur qui connaît cet attribut, puis déclarer le
  même profil sur chaque hôte collecteur et chaque hôte qui produit ces
  vues. Les nouveaux relevés portent déjà le nom du profil déclaré ;
  l'adoption rattache l'historique et les collecteurs encore sans profil.
  Sans déclaration, chaque série reste distincte. Si les lectures sans
  compte proviennent de plusieurs comptes réels, cette adoption globale
  n'est pas applicable.
* Isolement : sur un hôte à comptes, chaque harnais lancé (tour ou `ameesh
  attach`) part de l'environnement de l'exécuteur **sans** les variables
  d'identifiants — sources de clé de tous les comptes déclarés, et, quand un
  compte est choisi, `CLAUDE_CONFIG_DIR`, `CODEX_HOME`, `DSH_HOME`,
  `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `DEEPSEEK_API_KEY` — puis reçoit
  seulement celles du compte choisi. Le profil est revalidé à chaque
  lancement, garde de budget active ou non : un profil invalide refuse le
  tour (consigne remise en attente, diagnostic dans `ameesh show`).
* La bascule demande `ameesh migrate` (migration 0028). La configuration de
  l'hôte est relue à chaud (#1466) : un compte ajouté, retiré ou corrigé dans
  `config.json` prend effet au tour suivant, sans redémarrage de l'exécuteur.
  Seuls les comptes, les plafonds, les rôles critiques, le partage des comptes
  (`accounts_shared`, `account_sharing`, `account_registry`) et les binaires de
  harnais suivent la relecture : la base, le schéma, le dossier d'état et les
  cadences sont ceux du démarrage.
* Deux meshes sur un même hôte (#313, #2580) : un dossier d'identifiants, un
  fichier de clé ou une clé d'API déclaré aussi par un autre mesh est signalé
  (`ameesh doctor`, `ameesh accounts list`, alerte `account_shared`), et refusé
  avec `"accounts_shared": "refuse"` (ou `"account_sharing": "strict"`, #313)
  — voir [§ 8](#8-one-account-one-mesh-two-meshes-on-one-host-313-2580-decision-0033).
* Profils (#1466) : un exécuteur peut être lancé sur un fichier nommé
  (`AMEESH_CONFIG`) rangé sous `~/.config/ameesh/runner-profiles/`. Ce
  **profil** est une superposition : les clés qu'il ne déclare pas — les
  comptes, notamment — viennent de `config.json`. Une clé déclarée par le
  profil l'emporte (une isolation voulue se déclare), et l'écart est affiché
  par `ameesh doctor`, `ameesh accounts list` et le journal de l'exécuteur ;
  un profil qui écrase `accounts` est signalé en clair. `"inherit": false`
  rend un profil autonome ; `"inherit": true` force l'héritage ; une chaîne
  nomme une autre base. Un fichier hors du dossier des profils reste autonome :
  le banc de tests, et la configuration d'une autre base rangée à côté de
  `config.json`, ne lisent jamais la configuration du poste.

## 3. Suivre et forcer

```sh
ameesh accounts list [--json]          # comptes, dernier choix, état, jauges, pertes, bascules
ameesh accounts use claude secondaire --for 1h
                                       # forçage manuel, avec échéance (levé seul au seuil si un autre compte est libre)
ameesh accounts use claude secondaire --force "sonde de jauge" --for 15m
                                       # passe outre la garde (overage/rythme), raison journalisée
ameesh accounts auto [claude]          # retour au choix automatique (0034), avant l'échéance
ameesh accounts healthy claude secondaire  # compte reconnecté : déclaré sain (L226b)
ameesh cost report                     # dépense par agent, compte actif, jauges par compte
ameesh cost gauges                     # historique des jauges, par compte
ameesh cost balance --record           # soldes, par compte de clé d'API
ameesh budget                          # plafonds du mesh en vigueur, source, pauses (L70)
```

**Un forçage a une garde et une échéance (#3755).** `ameesh accounts use`
évalue le compte avant de le forcer : s'il est **en overage** ou **au-delà de
son rythme** (`accounts.evaluate`), la commande refuse, avec la raison et le
rappel de `--force`. Une retenue d'overage valide publiée par un autre runner
de l'hôte est relue **sous le verrou de placement**, dans la transaction de la
bascule : sans `--force` elle refuse sans rien écrire, avec `--force` elle est
levée dans la même transaction. `--force "raison"` passe outre — la raison est
journalisée
dans `account_switches` (visible dans les dernières bascules d'`ameesh accounts
list`) — mais ne change
rien à la garde du tour : un compte en overage ou au-delà de ses plafonds n'est
jamais lancé, même forcé. `--for DUREE` (défaut **1 h** : `90m`, `6h`, `2j`)
pose l'échéance du forçage ; au terme, le choix suivant le lit comme
automatique, le lève (journalisé `auto`, raison « forcing expired ») et reprend
la règle 0034. `--for 0` est refusé : pas de forçage sans échéance. `ameesh
accounts list` affiche l'échéance ; `ameesh accounts auto` rend la main avant
elle.

`ameesh accounts list` montre, pour chaque compte, la capacité **perdue à la
prochaine remise à zéro** de chaque fenêtre en cours si rien ne change, et le
compte que prendrait une nouvelle session, avec sa raison (la même que celle
du choix) :

```
harnais   compte         actif   état     jauges
claude    primaire       oui     ok       five_hour 37% (rythme 73%), seven_day 35% (rythme 45%)
                                          ↳ perdu à la remise à zéro si rien ne change : five_hour 63 % dans 1 h 21, seven_day 65 % dans 3 j 20 h
claude    secondaire             ok       —
                                          ↳ prochain choix pour une nouvelle session : sans relevé : non vérifié, premier (L117) ; avant : tertiaire sans relevé : non vérifié, primaire seven_day 35 % utilisé (rythme 45 %)
claude    tertiaire              ok       —
```

Le rythme affiché — et celui de la raison du prochain choix — est celui de la
pause d'un agent non critique : la réserve de 10 points gardée pour les agents
critiques est déduite (#2648), comme dans `accounts.choose`. Un compte encore
sous son plafond ordinaire mais dans cette bande est donc « limit » et n'est
pas proposé comme prochain choix.

La colonne « actif » est le **dernier compte choisi** pour une nouvelle
session (ligne `account_active`) : des agents dont la session est ouverte sur
un autre compte y restent tant qu'il est sous son seuil.

`accounts list` et `cost report` sont des lectures : ils n'écrivent aucun
relevé de jauge (L71) ; le relevé revient aux exécuteurs, avant chaque tour,
ou à `ameesh cost gauges`. Un relevé dont la fenêtre est échue (`resets_at`
passé) compte pour **0 %** : un compte inutilisé dont le dernier relevé date
d'une fenêtre close n'est plus jugé au seuil (il ne servait pas, donc son
relevé n'était jamais rafraîchi). L'affichage garde le dernier relevé :
« codex-300min 0% (rythme 80%, remise à zéro passée, dernier relevé 93%) ».
Un relevé **partiel** (une fenêtre remise à zéro, une autre ouverte) est au
contraire marqué `↳ reading not current: window reset since the reading
(unverified)` : la valeur lue n'est qu'une borne, et les pertes annoncées
sont tues tant que le relevé n'est pas rafraîchi (#1561). Chaque relevé porte
son horodatage (`read_at` : marqueur de compte Claude, date du journal Codex),
exposé par `accounts list --json` et l'instantané `progress`.

Les journaux Codex récents sont réconciliés fenêtre par fenêtre (#2839) :
un relevé hebdomadaire seul ne supprime plus une fenêtre courte encore
ouverte. Leur horodatage d'événement prime sur la date de modification du
fichier. À identité et forfait égaux, la dernière observation de chaque
fenêtre gagne, y compris si sa remise à zéro varie ou a lieu plus tôt.
Des relevés vivants qui portent des identités de créateur ou des
forfaits incompatibles rendent le compte ambigu :
il est refusé, même garde de rythme désactivée, et le choix cherche un autre
compte. `creator_account_id` ne suffit pas à attribuer un relevé : une session
reprise peut conserver son créateur alors que ses nouvelles jauges portent
un autre forfait. Aucune valeur d'identifiant n'apparaît dans le diagnostic.
Les fenêtres échues ne maintiennent pas ce refus.

#3034 : pour un compte déclaré, le `auth.json` du compte vivant est lu ; son
`id_token` nomme le forfait (`chatgpt_plan_type`). Si les relevés partagent
une identité unique, ceux d'un autre forfait ne comptent plus pour
l'ambiguïté ni dans les fenêtres, au lieu de rendre le compte inutilisable
après une bascule. Ce forfait n'est pas une certitude : un relevé d'un autre
forfait peut être plus récent que le dernier du forfait vivant. Aussi, si une
de ses fenêtres vivantes est saturée ou en overage, le compte reste refusé —
l'ignorer masquerait une pause ou une dépense. Une fenêtre de l'autre forfait
déjà remise à zéro ne maintient pas ce refus. Le jeton n'est jamais affiché ni
journalisé. Sans forfait vivant lisible (fichier absent, vide ou sans jeton),
ou avec plusieurs identités de créateur, le refus d'ambiguïté reste la règle ;
le compte non déclaré garde le comportement précédent.
Sans compte déclaré, l'exécuteur met l'agent en pause avec la raison
d'ambiguïté ; le rapport de coût affiche cette raison avec des jauges vides.
La lecture porte sur les 32 journaux les plus récemment modifiés, avec
256 Kio de fin de fichier et 64 Kio de métadonnées au maximum par journal,
puis une seule réconciliation. Si une fenêtre attendue reste introuvable
dans un échantillon tronqué, le compte est refusé comme relevé incomplet :
une limite de lecture ne doit pas masquer une fenêtre saturée. Le forfait
pro peut ne publier que la semaine.

#3044 : un compte non-pro **au repos** — aucun relevé plus récent que sa
fenêtre courte (5 h) — ne peut plus produire de courte vivante. Quand une
courte a été retenue, échue, elle compte pour 0 % (L71) et le relevé est
accepté au lieu d'être refusé pour toujours : jamais choisi, le compte ne
serait jamais rafraîchi. La relaxation est bornée : un relevé récent (moins
d'une fenêtre courte) sans courte vivante, ou un échantillon sans aucune
courte, garde le refus #2843 — une courte saturée peut se cacher hors de
l'échantillon borné, et le format n'est pas prouvé sans courte observée.

#3122 : ce repos ne se prouve que sur les journaux **lus**. Un journal écarté
du scan (33e par date), un préfixe tronqué (> 256 Kio) ou un journal illisible
peut porter une courte vivante : sa date de fichier borne ce qu'il a pu
observer, et le repos n'est accepté que si le plus récent de ces journaux est
plus vieux qu'une fenêtre courte. Une date inconnue interdit le repos, même
quand le reste de l'échantillon est au repos.

Depuis la tranche 3 de #1580 (15/10/2026), les jauges d'un compte Claude
peuvent venir du fichier du widget omarchy (`~/.local/state/omarchy/agents/
usage/claude*.json`, dossier réglable par l'hôte avec
`AMEESH_USAGE_WIDGET_DIR`), quand il est **frais** : il porte les seaux
**propres à un modèle** (le « Fable Weekly » d'un compte, que les journaux
`rate_limit_event` ne voient pas). Un instantané n'est lu que si `updatedAt`
(repli : date du fichier) n'est pas plus vieux que sa plus courte fenêtre —
une fenêtre courte remise à zéro depuis le relevé invaliderait aussi la
longue (#1561) ; sinon les journaux restent la source, et un hôte sans widget
ne change rien. Un seau de modèle (`title`, ex. « Fable Weekly ») ne compte
que pour une session **de ce modèle** ; un modèle inconnu l'ignore, et
`accounts list` les montre tous (la ligne d'état peut donc être retenue par un
seau de modèle).

Un relevé ne date pas de son écriture : un `rate_limit_event` ne porte aucun
instant, et le dater au `mtime` du journal faisait passer la dernière mesure
d'un agent arrêté pour une mesure fraîche (incident du 13/10 : seven_day 67 %
a écrasé 87 % et l'admission est passée sous le plafond). Chaque relevé Claude
porte donc sa **provenance** : l'instant de production — le ts du marqueur du
tour qui l'a publié, absent pour un flux sans marqueur — et le journal
d'origine. Un relevé dont une fenêtre était déjà close quand le tour a
commencé est un **rejeu** du harnais : il reste lisible (la fenêtre échue vaut
0 %, L71) mais n'est pas daté et ne remplace jamais une mesure datée. Les
lecteurs départagent par instant de production, jamais par l'heure
d'insertion ; l'historique partagé (0434) complète la lecture locale d'un
exécuteur, fenêtre par fenêtre, quand un autre état a publié plus frais. Une
fenêtre reprise de l'historique n'est jamais réécrite dans l'historique (pas
d'écho d'un hôte à l'autre) et `accounts list` indique son âge (« read … ago »)
quand elle n'a pas de lecture locale. Sur un exécuteur `backend=mediated`,
`operations.gauge_latest` n'est **pas** admise au contrat médié : la
réconciliation d'historique y est inerte et la lecture locale est rendue
silencieusement. C'est un choix documenté, pas une panne : admettre cette
lecture toucherait le contrat d'autorisation et demanderait deux avis.

À l'affichage, une jauge Claude sans instant de production — rejeu seul, ou
flux sans marqueur — porte « non daté : provenance inconnue » : sa valeur
reste lisible, mais elle n'est pas présentée comme une mesure fraîche. La
mention ne dit pas « périmé » : aucune preuve de péremption n'est supposée
(ni âge maximal, ni `mtime`, ni plafond), et l'admission ne change pas.
Le widget omarchy, qui date lui-même son relevé (`updatedAt`), n'est pas
concerné.

Chaque choix de compte est journalisé avec sa raison dans le journal de
l'exécuteur (`[agent] compte claude : secondaire — sans relevé : non vérifié,
premier (L117) ; avant : tertiaire sans relevé : non vérifié, primaire
seven_day 35 % utilisé (rythme 45 %)`, ou `— continuité :
la session reste sur primaire, sous son seuil`), une fois par changement de choix et non à
chaque sondage. Chaque changement du dernier choix est journalisé en base
(`account_switches`, type `bascule`, avec la même raison) et dans le fil de
l'équipe de l'agent qui l'a déclenché. Les retenues jusqu'à la remise à zéro
(`account_holds`) et les « retours au primaire » de 0027 §3 ne sont plus
posés : le choix par retard sur le rythme les remplace. Une
session reprise sous l'autre compte, ou tournée avec résumé, est dite dans le
fil. Codex n'est jamais portable : un changement de compte d'une session
Codex passe toujours par la rotation avec résumé (0027 §5), c'est pourquoi il
n'a lieu qu'au seuil. Tous les comptes au seuil : l'agent passe en pause
(`blocked`, « tous les comptes … au seuil »), comme avant L30.

Le compte d'origine de la session courante est enregistré
(`agent_registry.session_account`, L39) : l'exécuteur l'écrit avec l'id de
session, `ameesh adopt` l'écrit pour une session interactive adoptée (compte
sous lequel son fichier a été trouvé), et il s'efface avec la session. Le
marqueur du flux `events.jsonl` ne sert plus que de repli pour les sessions
antérieures. Quand ce compte est inutilisable (identifiants retirés, forfait
saturé) et que la session n'est pas portable, la rotation ne peut pas résumer
sous l'ancien compte : la session neuve s'ouvre sur un **brief de reprise
déterministe** (voir `ameesh resume` dans [EXPLOITATION.md](EXPLOITATION.md)).

## 4. Failover order and caps (L226b, owner decision 2026-10-07)

"Primaries last, capped": the harness's default account (`~/.claude`,
`~/.codex`, declared without `path`) is the owner's own and only serves as a
last resort. Each account takes these optional settings in the host's
`accounts` key (defaults in brackets):

| setting | meaning |
|---|---|
| `priority` [default account 100, others 50] | rank in the failover order, lower first; the pace (L117) only breaks ties |
| `cap` [0.50 for the default account, none otherwise] | fixed cap on **every** window: above it, the account is ineligible |
| `overage` [`auto`] | may the account be billed beyond its plan? `auto` reads it from the harness (Claude `overageStatus`, Codex `credits.has_credits`), `allowed` or `none` declare it |
| `overage_cap` [0.80] | fixed cap on every window of an account that may be billed beyond its plan |
| `short_window_cap` [none] | fixed cap replacing the pace cap on the short window (5 h) |
| `pace_multiplier` [1.0] | multiplies the **elapsed share** of the pace cap: `min(90 %, elapsed × m + 10 points)`. Set per account, no implicit value (#1075: `2.0` on the Codex primary to spend its plan reserve without a reset). The reserve, the fixed caps, the overage rules, the gauges and the reset dates are unchanged |
| `pace_ceiling` [0.90] | the account's own pace ceiling, replacing the 90 % of the guard (0019): the pace cap is `min(pace_ceiling, elapsed × pace_multiplier + 10 points)` (#1176) |
| `pace_ceiling_end` [none] | raised ceiling for the **end of the window**: within `pace_ceiling_end_hours` of `resets_at`, the ceiling rises to this value — only when the account's overage is *proven impossible* (see below). `none` (default): no raise, the base ceiling always holds |
| `pace_ceiling_end_hours` [6] | hours before `resets_at` where `pace_ceiling_end` applies; declared alone (without `pace_ceiling_end`), it is a configuration error |

A window is held to the lowest of: its pace cap (0019, or `short_window_cap`
on the short window), `cap`, and `overage_cap` when the account may be billed.
An account **in overage** (Claude `isUsingOverage`, Codex a window at 100 %
with credits) is never chosen, until the windows that ran out have reset (a
spent window with no reset date keeps it out). The caps apply to session
continuity too: a session on the primary leaves it once the primary is above
50 % of a window. A manual force posted while a worker switches accounts is
evaluated the same way: a forced account past its caps is not launched.
A forced account at its threshold (pace, caps or overage) does not hold the
agents while another healthy account is under its own: the force is lifted
(journaled `auto`) and the automatic choice runs (bureau, 2026-10-12). With
no other account free, the force holds and the agents pause. The lift is
fenced on the forced account that was read (#295): a human who forces another
account in between keeps their force, applied as is for that turn.
Since #3755 the command itself refuses to force an account already in overage
or above its pace (`--force "reason"` overrides, journaled), and every force
carries an expiry (`--for`, 1 h by default): at the deadline the next choice
reads it as automatic and lifts it (journaled `auto`). A valid overage hold
published by another runner of the host is re-read under the placement lock,
in the switch transaction: without `--force` it refuses and writes nothing;
with `--force` it is lifted in that same transaction. The expiry is fenced on
the forced account that was read, like a threshold lift: a concurrent force of
another account is kept.
With the budget guard off (`budget_usd_per_hour` 0) the pace no longer
pauses, but these rules still hold: an account in overage or past `cap` /
`overage_cap` is never chosen, and with no other account the agent is
paused (`budget : …`) rather than launched on it.

### End of window: the ceiling rises only when overage is impossible (#1176)

A plan window that ends with unused capacity loses it. `pace_ceiling_end`
lets the account spend that end of window, but only where no paid overage can
result. The raise applies to a window when `resets_at` is at most
`pace_ceiling_end_hours` away and the account's overage is **proven
impossible by a fresh reading**: the gauge reading itself, with an explicit
"not capable" flag (Claude `overageStatus` not allowed, Codex
`credits.has_credits` false) and at least one window still running — a
reading whose windows have all reset is stale (L71).

An unknown, partial or non-boolean flag is **not** a proof: a Codex `credits`
object with no `has_credits`/`unlimited`, or non-boolean values, leaves the
capability unknown (B1, #1176).

The account's `.claude.json` is **not** a source of proof (B2, #1176): its
`cachedExtraUsageDisabledReason` can date from another window, and the mtime
of the file dates the container, not the cached value — Claude Code rewrites
that file for unrelated reasons (adding a single foreign key was enough to
raise the ceiling while the overage capability stayed unknown). No fresh
proof tied to the value being demonstrable, the fallback is removed: unknown
stays unknown.

Anything else — flag absent, unknown, possible overage, stale proof, undated
window — keeps the base ceiling (`pace_ceiling`), and an account that may be
billed keeps `overage_cap` (80 %). `overage_impossible` is the single place
that decides this. The overage refusal (`isUsingOverage`) and the turn cut
are unchanged: the raise never lets a turn run in overage. A configuration
declaration (`overage: none`) is *not* a proof: only the harness's own fresh
reading counts.

`ameesh accounts list` shows the effective ceiling and its reason per gauge
(`seven_day 91% (pace 88%, ceiling 98%, end of window, overage impossible)`),
and the pause reason names it too. A pause held by the base ceiling but
lifted earlier by the raise announces the raise's hour (`until`), not the
reset.

A session opened before L30 (no account recorded) ran on the harness's
default folder: its health, gauges and caps are read under the declared
account of that folder (the primary), not under `(défaut)`.

The summary of a rotation (§ 3) runs on the **old** account only if that
account could still be chosen apart from its pace: not `failing` (§ 5), under
`cap` and `overage_cap`, not in overage, its gauges readable. Otherwise no
model call: the session resumes from the deterministic brief (L39). An
ordinary rotation (size, context cap, L11) passes the guards of a turn first
(budget and caps, authentication) and runs only on the session's own account;
otherwise it waits, and the account switch of the next turn rotates.

For the order "Claude tertiary > secondary > primary, Codex secondary >
primary" on a host that declared `primaire, secondaire, tertiaire`:

```json
"claude": [
  {"name": "primaire"},
  {"name": "secondaire", "path": "/home/me/.claude-secondary", "priority": 20},
  {"name": "tertiaire", "path": "/home/me/.claude-tertiary", "priority": 10}
]
```

Bureau's local sprint patch of 2026-10-11 (`~/.config/ameesh/sprint.json`,
`short_window_cap` 0.75 for the non-default accounts, bureau 2026-10-12) is the setting
`"short_window_cap": 0.75` on those accounts.

`ameesh accounts list --json` shows `priority`, `overage` (setting, capable,
in use) and, per gauge, the `cap` the window is held to, the effective
`pace_ceiling` and its `ceiling_reason` (empty for the base ceiling, #1176).

## 5. Authentication failure: `auth_blocked` (L226b)

A credential can stop working without notice: expired or revoked OAuth token,
"Please run /login", 401 `authentication_error`, gcloud "Reauthentication
failed". Before L226b the turn failed, the consigne went back to pending, and
the agent replayed the same turn until the stop after fast failures (L48).

**Credential health** (table `credential_health`, migration 0060): one row
per (host, harness, account); `account` is the declared account name, `''`
for the default credential of a harness without accounts. `state` is
`healthy`, `failing` or `unknown`; `source` says who wrote it: the sentinel
(`sentinel`, L226a), a failed turn (`turn`) or a human (`manual`).
`failing_since` keeps the start of the failure, `expires_at` the forecast
expiry the sentinel reports. Each `failing` report also stamps
`last_failure_at`, counts one more in `failures`, and keeps
`failing_fingerprint`: a truncated SHA-256 of the credential then in place
(refresh token of `.credentials.json` or `auth.json`, the key of an API-key
account, the whole gcloud, gh or scw store), which tells two credentials
apart without revealing either. The fingerprint is never in the supervisor
contract (it lives in migration 0068, additive to 0060). A failed turn
records the fingerprint of the credential it was launched with, read from
the child's environment at launch: an API key is the exact value handed to
the child, even if its key file is replaced during the turn.

**End of turn.** A failed turn whose error, short answer (600 characters at
most) or stderr tail (this turn's output only: `stderr.log` is appended to by
every turn) is an authentication failure
(`ameesh.auth_failure.classify`) is neither a fast failure nor replayed: the
turn's account is marked `failing` (source `turn`), the consigne goes back to
pending, the agent goes `blocked` with `status_text` "`auth_blocked:
<harness>/<account>`" and `last_error` "`auth_blocked: <reason>`". A turn that
fails otherwise (429, context, exit code) takes the usual path.

**Failover.** The account choice (§ 2 and § 4, `accounts.choose`) is
restricted to **healthy** accounts: a `failing` account is never chosen, not
even by session continuity. The next turn starts on a healthy account, with
the usual session resume or rotation, and the block is lifted.

**Never a paid fallback**: an account above its cap stays out, and a
harness with a plan account never falls back to a pay-per-token API key
(`type: api_key_env`) automatically (`ameesh accounts use` still can). Among
the API keys of a harness without plan accounts, failover stays possible.

**No healthy account under its cap**: the agent stays `auth_blocked`. It
consumes neither consigne nor mail, no turn is tried, and the `auth_blocked`
alert (pushed by default, urgent) goes to the responsible human. It resumes on
its own as soon as its account, or another, is healthy again. A forced account
(`ameesh accounts use`) that fails is not replaced: the agent stays blocked
until `ameesh accounts auto` or the reconnection. If the healthy accounts are
at their cap but a failing one is not, it is `auth_blocked`: reconnecting
unblocks. If every account is at its cap, failing or not, it is the usual
budget pause; once it lifts, an account still failing puts the
`auth_blocked` mark (and its alert) back.

**Recovery.** After the reconnection (`claude /login` in the account's
folder, `gcloud auth login`…), the L226a sentinel sees the account healthy
and records it — only if its probe is healthy AND the credential's
fingerprint differs from the last failure's, whenever that credential was
installed (a turn launched with A can fail after the owner installed B: B is
cleared). Rewriting the same revoked token, or a failure recorded without
a fingerprint, is no proof: the account stays `failing`. A failure a turn
detected names no fix command: the sentinel's next probe adds the account's
command to its reason (the owner's alert shows it), without touching the
failure itself. The sentinel's write
is conditional on the `failures` count it read, so a failure a turn records
during the probe is never overwritten. Without the sentinel, or in those
cases, declare it by hand:

```sh
ameesh accounts healthy claude secondaire   # declared account
ameesh accounts healthy claude              # harness without declared accounts
```

`ameesh accounts list --json` shows the table (`credentials`).

## 6. Credential sentinel (L226a)

Every 10 minutes, each host probes every credential its agents use and
writes the result to `credential_health` (`source = 'sentinel'`):

| harness | probe | forecast expiry | fix command in the alert |
|---|---|---|---|
| `claude` | per declared account (or default dir): `claude auth status` in its config dir says `loggedIn`. An expired access token (`expiresAt`) is **not** a failure: it lapses after a short idle time and is refreshed on use | `refreshTokenExpiresAt` | `CLAUDE_CONFIG_DIR=<dir> claude auth login` |
| `codex` | per declared account: `auth.json` has a refresh token or an API key | — | `CODEX_HOME=<dir> codex login` |
| `gcloud` | `gcloud auth print-access-token` | last interactive login (mtime of `credentials.db`) + Workspace session length | `gcloud auth login --update-adc` |
| `gh` | `gh auth status` | — | `gh auth login -h github.com` |
| `scw` | `scw account project list` | — | `scw init` |

A harness whose binary is missing, or whose default credential file is
absent without declared accounts, is not used on the host: it is skipped. A
probe that cannot conclude (timeout, unreadable file) writes `unknown`,
which is never a verdict. The same goes for a CLI probe (gcloud, gh, scw)
that exits non-zero without saying its credential is refused: a network
error (DNS, timeout, 5xx) or an unrecognized output is `unknown`, never
`failing` — a `failing` credential only clears with another credential, so
a transient error must not become one. The whole output is read (a refusal
can come before many lines of other accounts). The fingerprint of gh and scw
is that of the credential in effect: `GH_TOKEN` / `GITHUB_TOKEN`, or
`SCW_SECRET_KEY`, when set (they override the store), else the store file. The sentinel reads names, paths and dates only and
never prints a secret. An `api_key_env` account is checked for the presence
of its key.

Alerts, computed by `ameesh notify` from the table (all hosts):

* `credential_failing` (pushed by default, urgent): a credential is
  `failing`, from a probe or from a refused turn (L226b). Its detail ends with
  the exact fix command. There is one alert per (host, harness, account,
  start of failure), and it resolves when the credential is `healthy` again;
* `credential_expiring` (pushed by default): a healthy credential's forecast
  expiry is less than `warn_hours` away (default 24 h). This warns before
  gcloud asks to log in again in the middle of a release.

Setup on each host (systemd user units, same environment as the runner):

```sh
cp deploy/systemd/ameesh-credentials.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now ameesh-credentials.timer
ameesh accounts check --dry-run      # probe now, write nothing
```

Optional host configuration, key `credential_sentinel`:

```json
"credential_sentinel": {"probes": ["claude", "codex", "gcloud", "gh", "scw"],
                        "gcloud_session_hours": 16, "warn_hours": 24}
```

`gcloud_session_hours` must match the Workspace admin setting
(*Security > Google Cloud session control*). The default is 16 h.

### Long-lived Claude token (`claude setup-token`): study and recommendation

`claude setup-token` opens the same browser authorization as `/login` and
prints a **one-year** OAuth token tied to the subscription (Pro, Max, Team,
Enterprise). The token is not saved anywhere; the harness reads it from
`CLAUDE_CODE_OAUTH_TOKEN`, which takes precedence over the `/login`
credentials of the config dir. It only removes the dependency on the OAuth
refresh of `.credentials.json`
([docs](https://code.claude.com/docs/en/authentication#generate-a-long-lived-token)).

Limits found:

* scope `user:inference` only: model requests work (what agents need), but not
  `claude usage`, Remote Control or claude.ai connectors;
* `claude auth status` then shows `authMethod: oauth_token` and no org or
  email, and has been reported `loggedIn: true` while requests got a 401. With
  such a token, the sentinel's probe proves nothing, and only a refused turn
  (L226b, `source = 'turn'`) reveals a dead token;
* no documented expiry API: the sentinel cannot forecast the expiry unless the
  creation date is declared;
* revocation is done on the web (claude.ai settings), and the CLI cannot list
  tokens;
* a token can be minted for a different organization than intended, and that
  cannot be checked afterwards;
* ameesh has no account type for it yet. The `api_key_env` type would pass
  the variable, but ameesh counts such an account as **paid per token**. It
  would then be excluded from plan failover (rule "never extra spend") and
  billed in the ledger as paid usage, which is wrong.

**Recommendation: not yet for all agents; pilot on one account after a small
lot.** That lot adds an account type `oauth_token` with these fields:
`config_dir` (sessions and settings), `key_file` (0600, never in the
configuration), and `token_created` (the sentinel's forecast is creation +
365 days). It counts as a plan account (`credential_mode: subscription`). The
pilot then checks that `rate_limit_event` still feeds the gauges. Until then,
the sentinel's `auth status` probe and the L226b turn detection cover the
OAuth refresh failure mode.

Owner steps, once per account (only the owner can approve the login):

```sh
CLAUDE_CONFIG_DIR=~/.claude-secondary claude setup-token
# browser: sign in with THAT account's email, approve; the token prints once
install -m 600 /dev/null ~/.config/ameesh/keys/claude-secondary.token
$EDITOR ~/.config/ameesh/keys/claude-secondary.token   # paste the token, save
date -I   # note the creation date for `token_created`
```

Then declare the account with the `oauth_token` type (after the lot above).
To rotate a token, run the same steps again and revoke the old token in the
claude.ai settings.

### Git HTTPS: the helper written by `gh auth setup-git` carries an absolute path

The provisioning step of the [L232 study](design/etudes/l232-autonomous-host-provisioning.md)
(`gh auth setup-git`) writes in the global config, for `https://github.com`
and `https://gist.github.com`, the empty reset helper followed by
`!<absolute path of the gh binary that ran it> auth git-credential`. That
path is frozen at the moment of the command: a config copied from another
host (a VM's `/home/ubuntu/.local/bin/gh`) or a `gh` moved since (mise
update) leaves a helper that no longer exists. Every HTTPS GitHub operation
then fails on the credential lookup while `gh auth status` stays healthy —
the sentinel probes `gh`, not `git`, so it sees nothing (incident #1063).

Repair on the host itself. `git config --global` reads and writes a single
file: `GIT_CONFIG_GLOBAL` when it is set, otherwise the first existing of
`~/.gitconfig` and the XDG file (`$XDG_CONFIG_HOME/git/config`, default
`~/.config/git/config`), otherwise `~/.gitconfig`, which the first write
creates. `--global` reads that file alone and does not follow `include.*`
directives unless `--includes` is given, so the faulty entry can live in a
file the repair never writes (the shadowed global file, an included file): the
first command below lists every entry with its file and scope, and the repair
saves the file it writes plus every global file that carries a credential
entry. It runs in a subshell with `set -e`, so a failed copy stops it before
the first modification; the empty value is what clears a helper inherited from
another scope:

```sh
# locate every credential entry, with file and scope (global, XDG, included)
git config --show-origin --show-scope --get-regexp '^credential\.'

# repair, in a subshell with `set -e`: a failed backup stops the procedure
(
set -e

# file `git config --global` reads and writes, i.e. the file repaired
if [ -n "${GIT_CONFIG_GLOBAL+x}" ]; then
  target=$GIT_CONFIG_GLOBAL
elif [ -f "$HOME/.gitconfig" ]; then
  target=$HOME/.gitconfig
elif [ -f "${XDG_CONFIG_HOME:-$HOME/.config}/git/config" ]; then
  target=${XDG_CONFIG_HOME:-$HOME/.config}/git/config
else
  target=$HOME/.gitconfig
fi

# save the repaired file and every global file carrying a credential entry
stamp=$(date +%Y%m%d-%H%M%S)
{ printf '%s\n' "$target"
  git config --show-origin --show-scope --get-regexp '^credential\.' |
    awk -F'\t' '$1 == "global" && $2 ~ /^file:/ {sub(/^file:/,"",$2); print $2}'
} | sort -u | while IFS= read -r f; do
  [ -e "$f" ] || continue
  cp -p -- "$f" "$f.bak-$stamp"
done

for url in https://github.com https://gist.github.com; do
  git config --global --replace-all "credential.$url.helper" ""
  git config --global --add "credential.$url.helper" '!gh auth git-credential'
done
GIT_TERMINAL_PROMPT=0 git ls-remote https://github.com/<org>/<private-repo> HEAD >/dev/null && echo ok
)
```

The value is single-quoted on purpose: pasted into an interactive shell, a
double-quoted `!gh` triggers history expansion — `event not found` when no `gh`
command is in history, otherwise the previous `gh` command is substituted and
the helper is written broken.

Re-running `gh auth setup-git` on the host has the same effect, with that
host's current absolute path. The PATH form above survives a later move of
`gh`, as long as `git` runs with a PATH that finds it (the runner units set
the mise shims explicitly). Never copy `~/.gitconfig` between two hosts:
re-run the command on the target and verify it with a private
`git ls-remote`.


## 7. Critical agents: never paused while an account has headroom (L242b)

Owner requirement (2026-10-12, 09:55 and 10:00). On 2026-10-12 at 09:50 the
orchestrator and mesh-design were paused on budget.

**Who is critical** (L242c). An agent is critical by a role on its canon
Agent card (`roles:`, manaty/home#25), not by its name. The critical roles
are set per organisation, by `critical_roles` in its `federation.yaml` — an
organisation may leave `relecture` out (manaty/home#179):

```yaml
critical_roles: [orchestrateur, conception, auditeur]
```

When the key is absent, the code default applies: `orchestrateur`,
`conception`, `auditeur` and `relecture`. An empty list makes no role
critical. A role and its usual synonym match each other both ways:
`orchestrateur` and `orchestrator`, `conception` and `design`, `auditeur` and
`auditor`, `relecture` and `review` or `reviewer`. The `auditeur` agent
without a card is critical while `auditeur` is a critical role.

A role counts only on the card in the agent's own canon, against that
canon's `critical_roles`, and only as the canon validated it: a `roles:`
value that `ameesh canon check` rejects (`agent-roles-invalid`) gives no
role. A conflicting card with the same name in another canon never makes an
agent critical.

The same list decides the host withdrawal: the drain's critical set
(`host_lifecycle.critical_names`, which adds `critical_agents`) follows the
same L242c reading — the canon's roles when it is readable, the last recorded
verdict when it is not, and critical when no verdict is known — so an agent is
evacuated first and gets the short loss window only as its own organisation
defines it. A reviewer is not critical there either when `relecture` is not
one of its critical roles, and the `auditeur` persona is critical without a
card while `auditeur` is a critical role.

**Unreadable canon.** When the agent's own canon cannot be loaded or read,
or was read only in part (a card or the manifest could not be read:
`canon-git-error`, `frontmatter-invalid` or `federation-invalid` errors), its
criticality is never lowered silently: the last verdict read is kept, and
without one (never read, or absent from the cache) the agent counts as
critical. An agent whose owner canon is configured on no canon of the host
(every configured canon loaded) is a known, permanent state, not an outage:
it is not critical, and the runner (or `ameesh resume`) warns once per
process, naming it. The last verdicts are kept in the host's
state (`critical-known/known.json`), so a restarted runner and `ameesh resume`
read them too; `ameesh resume` refreshes them when the canon is readable,
and the runner compares each verdict it reads with the disk (never with its
own memory). The cache fails closed: missing (deleted or hidden), unreadable,
corrupt, an invalid value, or a file that cannot be trusted (a symbolic
link, not a regular file, not owned by the runner's user, readable by
others) means "last verdict unknown", which counts as critical. Its
directory `critical-known/` is opened without following links, relative to
the state directory, and must be owned by the runner's user with mode 0700;
the data is opened relative to it, so a replaced or linked parent redirects
nothing. It is written under a lock on that directory itself (`flock` on the
descriptor already opened: no lock path to replace; only the data file is
renamed), read-modify-write, through
an exclusive temporary file, fsync, rename, fsync of the directory, mode
0600; a corrupt file is never rewritten (delete it to start again: while it
is missing, every agent counts as critical) and invalid values are kept as
they are. The runner trusts a verdict only once written, and tries again
at the next poll otherwise. Two residual risks are accepted: the state
directory itself is opened following links and without an owner check, and
the cache directory could be replaced between its opening and the lock (the
write then lands in the detached directory). Both need a malicious action
under the runner's own user in its state directory, which is outside the
threat model. `ameesh alerts` raises `critical_roles_unreadable` for each
configured canon that cannot be read in full on the host; it stays one
alert while the outage lasts.

Two declarations still make an agent critical whatever its roles:

* the host's `critical_agents` setting (list), or `AMEESH_CRITICAL_AGENTS`
  (comma-separated): the explicit override, for an agent whose card does
  not (yet) say it;
* `AMEESH_ALERT_ORCHESTRATORS`.

`ameesh resume`, when it plans the account of the next session, judges an
agent the same way.

**Per-agent choice.** For a critical agent, the account choice of § 2–4 is
made for that agent alone:

* it never writes the shared last choice (`account_active`), which is what
  new sessions of the other agents use. This holds even when that row is
  missing, or names an account removed from the configuration: the critical
  agent starts from the first account for itself, and leaves the row and its
  `config` switch to the next non-critical turn;
* it never lifts a manual force. A forced account under its threshold
  applies to critical agents too. A forced account at its threshold does
  not hold them while another account has headroom; the force stays in
  place for the others.

A switch is portable when the session moves within the same harness (Claude
with a shared `projects/`, § 1). It is then made in the same session, before
the turn and before any pause. The runner journal and the team thread say
"Account switch claude for this critical agent only: A → B (…)", once per
change. No `account_switches` row is written, since the shared choice did not
change.

**Fallback order for a critical agent at its threshold**:

1. another plan account of the same harness with headroom (§ 4 order);
2. the harness's default account (`~/.claude`) beyond its implicit 50 % cap.
   The rules for this step:
   * it is for critical agents only, and comes last;
   * the account is never used in overage;
   * its pace cap and `overage_cap` still hold, even with the budget guard
     disabled;
   * a `cap` written explicitly in the configuration still holds.

   As soon as another account has headroom again (for example after its
   window resets), the agent leaves the default account: continuity only keeps
   a session on an account that is under its ordinary threshold;
3. another harness, in the order of `critical_fallback_harnesses` (host
   configuration, `AMEESH_CRITICAL_FALLBACK_HARNESSES`; default `codex`, then
   `deepseek`: a plan harness, then an API-key one). The first one that has
   headroom is used:
   * it must be installed on the host;
   * it must have an account under its threshold. With declared accounts,
     this is a choice for the agent alone, as in steps 1–2; without, its
     default plan must be under its pace;
   * if it is paid per token, the mesh hourly cap must still hold. A critical
     agent has it in full, since non-critical agents stop at
     `noncritical_hourly_share`.

   The switch goes through a rotation with summary. The summary is made
   under the old harness and the session's account, the only ones that can
   resume it; when that is impossible, a deterministic brief is used (L39).
   A paid-per-token old harness must still hold the mesh caps for the
   summary, like a turn; otherwise the brief is used. The summary (or
   brief) is kept in the agent's state (`resume-pending.json`) before the
   old session is forgotten: a runner restarted before the new session
   starts still opens it with the summary. It is dropped only once the new
   session has reported its id (or its turn completed): a harness that dies
   at launch leaves it for the next turn. If it cannot be written (disk
   full), the rotation does not happen: the old session is kept and the
   turn waits. An ordinary rotation (L11) never
   runs while the next turn would go to another harness than the session's:
   the switch does it.
   A fresh session then starts under the new harness. The agent's model,
   effort and tier do not follow it: the borrowed harness uses its own
   defaults. Each turn is counted under the harness it ran on (ledger,
   mesh cap): that harness is written to the agent's state before the
   accounting marker, and the turn does not start if it cannot be. The
   thread says "its turns run under codex/<account> until its
   own account has headroom again" and "Harness switch: …".

   **Coming back.** Before every turn the agent's own harness is evaluated
   first. While the agent runs under a borrowed harness, it comes back only
   when one of its accounts has the headroom of a non-critical agent: the
   reserve of 10 points under the pace (`accounts.RESERVE`) and
   `noncritical_hourly_share` of the hourly cap, including on the default
   account taken beyond its 50 % cap (step 2). Without that margin, each
   crossing of a rising pace cap would cost a summary and a rotation. If no
   borrowed harness has headroom left, its own account is used without the
   margin rather than a pause. The return is the same rotation, with the
   summary made under the borrowed harness;
4. pause, once all of the above are exhausted.

**Reserve for the critical agents** (L242b (2)). The other agents stop
earlier, so that the margin left is for the critical ones:

* on a plan, a non-critical agent stops 10 points below the pace cap
  (`accounts.RESERVE`): `max(min(80 %, elapsed × m), 10 %)` — the pace cap
  `min(90 %, elapsed × m + 10)` minus the reserve, floored at the 10-point
  margin. The reserve never blocks the start of a window (#777): while
  `elapsed × m` is at most the 10-point margin, the threshold is the margin,
  not `elapsed × m` (the plain reserve subtraction, which would block at
  1 %); beyond it the threshold follows `elapsed × m`, and the end-of-window
  cap is 80 % (90 % − 10), not more (mesh-design, 2026-10-12; #1028). The
  short window cap, when set, is lowered the same way, and never exceeds that
  threshold for a non-critical agent. The fixed caps (50 % of the primary,
  `cap`, `overage_cap`) are not: they hold for everyone. The reason then says
  "10 points kept for critical agents". This holds with or without declared
  accounts. It also holds on a host with no critical agent: the reserve is
  then simply not used;
* on pay-per-token, a non-critical agent stops at `noncritical_hourly_share`
  of the mesh hourly cap. The setting is in the host configuration
  (`AMEESH_NONCRITICAL_HOURLY_SHARE`; between 0 and 1; default 0.75, a
  mesh-design decision of 2026-10-12): 15 $ of 20 $. A critical agent goes up
  to the full cap.

**The mesh hourly cap counts the real spend** (mesh-design decision,
2026-10-12). The pay-per-token spend of the last 60 minutes is the real drop
of the provider balances, **account by account** (each API key has its own
balance series):

* a drop between two readings counts only for its part inside the hour. It
  is split like the ledger estimate of that account over the interval, or in
  proportion to time when the interval carries less than 0.50 $ of estimate;
* outside the interval covered by an account's readings, its ledger estimate
  counts, corrected by its real/estimated ratio. The ratio is used only when
  the covered interval carries at least 0.50 $ of estimate, and it never goes
  below 0.1 (`cost.PAID_RATIO_FLOOR`);
* an account without readings counts its estimate as is: an account with
  readings never covers the others. A balance series with no account name
  covers the ledger lines that no named series covers.

Unreadable balances fall back to the estimate. `ameesh budget` shows the
counted amount next to the estimate.

**A budget pause is shown as one.** The pause reason starts with the account
that frees first: "pause (account secondaire, five_hour 96 %) until 14:20;
all claude accounts at threshold — …". It is the agent's `status_text`
("budget : pause (…) until 14:20; …"). The announced time is the real one: a
window held by the pace alone frees when the elapsed part catches up with the
usage (the guard is re-evaluated on every turn, #898), while a usage at the
pace ceiling (90 %, less the reserve kept for critical agents) only falls back
at the window reset. An account held by several windows frees with the last of
them: the label names that window, and the date when it is not today ("until
14/10 09:00"). Without declared accounts the
account is `(default)`, the harness's own. `ameesh progress` and the alerts read
`paused`, never `stopped`, for a budget pause. `ameesh attach` says it.

**Urgent alert** (L242b (5)). When a critical agent is paused on budget
anyway, the runner marks its status text ("budget : critical agent: …").
`ameesh alerts` then raises `critical_budget_pause`. The alert is pushed by
default and urgent, and is mailed (urgent) to the orchestrators of the
agent's team and to the auditors.

## 8. One account, one mesh: two meshes on one host (#313, #2580, decision 0033)

Decision [0033](design/decisions/0033-isolated-meshes-hosting-and-identity.md):
a device serving two organisations runs two meshes, with two sets of accounts
and secrets. On 2026-10-10 the two meshes of pc-smichea still declared the
same Claude (primary, secondary, tertiary) and Codex credential folders:
shared quota, sessions in the same folders. Each mesh has its own database, so
neither the database nor #1883 (which compares the configurations of one
mesh) can see it. The host can.

**Registry of the host.** Every process that publishes account identities
(#1883, `_publish_identities`) also claims the credentials of the accounts it
declares, in a registry shared by the meshes of the host and the user:
`account_registry` in the host configuration, by default
`~/.local/state/ameesh-host/accounts` (under `$XDG_STATE_HOME` when it is
set). One host registry: this path is the one #313 introduced, kept by
mesh-design's decision of 2026-10-19 for #2580; the earlier
`$XDG_STATE_HOME/ameesh/account-claims` path in the lot's brief is abandoned.
The runner
also claims at startup and then every 10 minutes. A credential is the resolved
location (`realpath`, links included) of a `config_dir` account's folder (the
harness default folder for an account without `path`), of a `key_file`, or the
fingerprint of a `key_env` value — never the value itself. Each credential gets
a folder named by its fingerprint; each mesh writes one claim file there per
configuration, named by its mesh identity and by a fingerprint of its
configuration file. Two configurations of the **same** mesh (runner profiles,
#1466) are never a conflict. A claim lives one hour after it is written and is
removed when its configuration no longer declares the credential (a corrected
configuration takes effect with the hot reload). The registry holds
fingerprints, account names, the host, the configuration file's name (without
its folder) and the policy: never a key, never a path, never what a credential
contains. Simulation (`ameesh resume`, L39) writes nothing.

**Identity of the mesh.** The claim's mesh identity is **read from the
database** and **stable**: the cluster's `system_identifier` and the random
identifier of the event journal (`ameesh_events_journal`, 0448), written
`mesh:<system_identifier>:j:<journal>` — read from
`ameesh_events_cluster()` and normalised, or from those two parts directly.
The **timeline** is deliberately not part of it: a failover or a
point-in-time recovery starts a new timeline while the journal, and so the
mesh, stays the same (#2592). There is only this one form: it is never
derived from the DSN (a mesh reached through pgbouncer and the same mesh over
direct TCP are one mesh), and there is no second fallback key that would vary
with the role's privileges; if the stable identity cannot be read, no claim
is written rather than a claim under a different identity. Two databases or
two schemas are two meshes. This is what #1883 could not see: its
`account_identities` compare fingerprints inside ONE database.

**Identity of the machine** (#2593, 2026-10-19, N3 of the review of #2580).
A claim also carries a fingerprint of the machine (`/etc/machine-id`, else the
system hostname; the identifier itself is never written). Claims are matched
on it, never on the configuration's `host` (`AMEESH_HOST`): two meshes of one
workstation that name the host differently still see each other. The registry
is already local to the machine and the user; the fingerprint only guards a
registry shared between machines (a common `$XDG_STATE_HOME` or
`account_registry`), where one path names two different credentials. A claim
written before #2593 has no machine fingerprint and is still matched on `host`.

**Report or refuse.** A credential claimed, within the hour, by **another**
mesh of the same host is shared. By default this mesh only reports it:

* the runner logs it at startup and whenever the set changes ("claude account
  secondaire: credentials also declared by another mesh of this host:
  config-ancienne-base.json (mesh 1a2b3c4d) — …");
* `ameesh alerts` raises `account_shared` (pushed by default), one per account
  and per day, until the other mesh stops declaring it;
* `ameesh doctor` prints an `accounts` line and `ameesh accounts list` adds
  "shared with another mesh of the host" under the account.

Refusal is behind a host key, to be enabled after the separation of #65:

* `accounts_shared: "refuse"` (#2580), the host-level key;
* `account_sharing: "strict"` (#313), the mesh-level equivalent kept for
  compatibility.

With either, this mesh **refuses** the account. The refusal is a problem of the
profile, like missing credentials: the choice never ranks it, with the budget
guard on or off, a turn or `ameesh attach` on it is not started, and
`ameesh accounts list` gives the reason. The policy belongs to each mesh: to
keep a credential in mesh A and give it up in mesh B, set `refuse`/`strict` in
B only. With it in both, neither uses the account until the configurations are
separated. Any other value is refused when the configuration is read. The
policy and the registry follow the hot reload (#1466, #2571): a switch from
`report` to `refuse`/`strict`, or a moved `account_registry`, takes effect at
the next turn and the next claim pass, without restarting the runner.

```json
{"accounts_shared": "refuse"}
```

**Limits.** Two **different** folders logged in to the same provider account
are not seen, because ameesh never reads a credential. A `key_env` variable
that is absent from the executor's environment has no value to fingerprint
(the launch would fail for the same reason). The separation itself stays the
owner's: lot #65 (accounts per organisation).

## Fenêtre courte du fournisseur (#3779)

Le forfait Codex n'expose qu'un bucket hebdomadaire (`codex`, 10080 min). Le
fournisseur applique en plus un **bucket de rafale/concurrence non documenté,
au niveau du compte** : plusieurs sessions reçoivent 429 sur la première
requête d'un tour alors que la jauge hebdomadaire est loin de son plafond
(openai/codex#41702, holon-run/holon#3369). Les runners le suivent maintenant
ensemble :

* `account_rate_events` journalise chaque 429 par `(harnais, compte)` ; le
  cooldown partagé vaut 4 min après le premier refus, doublé à chaque refus
  supplémentaire dans les 15 min, plafonné à 15 min. Il retarde le **début**
  des tours, jamais un tour en cours ;
* `account_turn_starts` porte les débuts de tour du compte ; l'écart minimal
  par défaut est de **30 s**, le plafond de tours actifs de **2** (Codex). Le
  plafond compte les places d'`account_turn_slots`, qui portent le harnais et
  le compte **effectifs** du tour : un agent Claude/DeepSeek qui emprunte Codex
  (repli L242b) compte dans le plafond Codex. La place n'est retenue que si le
  bail est **vivant** (`lease_expires_at` futur) : un hôte tombé ne bloque pas
  le compte pour toujours. L'admission est atomique sous un verrou par compte :
  `begin_turn`, la place et le journal du départ sont dans la même transaction
  que la relecture du cooldown, de l'écart et du plafond, donc deux runners
  d'hôtes différents ne peuvent pas se glisser ensemble sous le plafond, même
  avec un écart nul ;
* `ameesh accounts list` affiche la jauge `codex-short-window` (texte et
  `--json`) : tours actifs, écart de début, 429 récents et cooldown.

Les deux seuils se règlent par profil de compte, comme les autres seuils de
compte, sans redémarrage :

```json
{"accounts": {"codex": [
  {"name": "primaire", "max_concurrent": 2, "min_start_gap_s": 30}
]}}
```

`max_concurrent: 0` ou `min_start_gap_s: 0` désactivent la garde
correspondante ; l'absence des clés prend les défauts du harnais. Un exécuteur
médié sans le domaine `account_windows` ignore la garde (contrat fermé).
