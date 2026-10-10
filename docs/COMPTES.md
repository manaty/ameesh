# Comptes multiples par fournisseur (L30, L74)

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
   `min(90 %, part écoulée + 10 points)`, décision 0019), celui dont **la
   capacité inutilisée expire le plus tôt** : la remise à zéro la plus proche
   parmi ses fenêtres en cours (5 h, 7 jours…). Un compte sans fenêtre en
   cours (relevé échu, non daté, ou aucun relevé) n'a rien qui expire : il
   passe après. À égalité, l'ordre déclaré.
4. Tous les comptes au seuil : pause.

Un relevé dont la fenêtre est échue (`resets_at` passé) compte pour **0 %**
(même règle que L71) : un compte resté à 93 % sur une fenêtre close n'est plus
écarté, il est essayé, et son premier tour rapporte sa jauge. Les jauges sont
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

* L'ordre départage les égalités d'échéance (et les comptes sans jauge
  datée, comme les clés d'API) : le premier est le primaire.
* `config_dir` sans `path` : le dossier que le harnais prendrait de lui-même
  dans l'environnement de l'exécuteur (`CLAUDE_CONFIG_DIR` / `CODEX_HOME`
  hérités s'ils sont posés, sinon `~/.claude` / `~/.codex`). Ses jauges Codex
  sont lues au même endroit, par le mécanisme commun : argument >
  `AMEESH_CODEX_SESSIONS` > `$CODEX_HOME/sessions` > `~/.codex/sessions` ; un
  compte déclaré lit `<son CODEX_HOME>/sessions`.
* Contrôles : dossier à l'utilisateur, **0700**, identifiants présents
  (`.credentials.json` pour Claude, `auth.json` pour Codex : présence
  seulement) ; fichier de clé **0600** ; variable de clé présente. Un compte
  qui échoue n'est jamais choisi, et `ameesh accounts list` dit pourquoi.
* `hourly_usd` : plafond horaire du compte (dépense des tours attribués au
  compte). Il est propre à l'hôte et s'ajoute aux plafonds du mesh, qui se
  règlent en base pour toutes les machines par `ameesh budget set` (L70,
  voir [EXPLOITATION.md](EXPLOITATION.md#plafonds-de-budget-du-mesh--ameesh-budget-l70)) ;
  un compte au seuil bascule, un plafond du mesh atteint met en pause. `min_balance` : le compte est au seuil quand son dernier solde
  relevé est au plus ce montant (relevé par l'exécuteur, ou
  `ameesh cost balance --record`).
* Isolement : sur un hôte à comptes, chaque harnais lancé (tour ou `ameesh
  attach`) part de l'environnement de l'exécuteur **sans** les variables
  d'identifiants — sources de clé de tous les comptes déclarés, et, quand un
  compte est choisi, `CLAUDE_CONFIG_DIR`, `CODEX_HOME`, `DSH_HOME`,
  `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `DEEPSEEK_API_KEY` — puis reçoit
  seulement celles du compte choisi. Le profil est revalidé à chaque
  lancement, garde de budget active ou non : un profil invalide refuse le
  tour (consigne remise en attente, diagnostic dans `ameesh show`).
* La bascule demande `ameesh migrate` (migration 0028) et un redémarrage de
  l'exécuteur pour relire la configuration.

## 3. Suivre et forcer

```sh
ameesh accounts list [--json]          # comptes, dernier choix, état, jauges, pertes, bascules
ameesh accounts use claude secondaire  # forçage manuel (plus de choix automatique)
ameesh accounts auto [claude]          # retour au choix automatique (0034)
ameesh cost report                     # dépense par agent, compte actif, jauges par compte
ameesh cost gauges                     # historique des jauges, par compte
ameesh cost balance --record           # soldes, par compte de clé d'API
ameesh budget                          # plafonds du mesh en vigueur, source, pauses (L70)
```

`ameesh accounts list` montre, pour chaque compte, la capacité **perdue à la
prochaine remise à zéro** de chaque fenêtre en cours si rien ne change, et le
compte que prendrait une nouvelle session, avec sa raison :

```
harnais   compte         actif   état     jauges
codex     primaire       oui     ok       codex-300min 10% (rythme 50%), codex-10080min 20% (rythme 53%)
                                          ↳ perdu à la remise à zéro si rien ne change : codex-300min 90 % dans 3 h, codex-10080min 80 % dans 4 j
codex     secondaire             ok       codex-300min 0% (rythme 90%), codex-10080min 5% (rythme 24%)
                                          ↳ perdu à la remise à zéro si rien ne change : codex-300min 100 % dans 52 min, codex-10080min 95 % dans 6 j
                                          ↳ prochain choix pour une nouvelle session : codex-300min expire dans 52 min, 0 % utilisé (avant primaire : …)
```

La colonne « actif » est le **dernier compte choisi** pour une nouvelle
session (ligne `account_active`) : des agents dont la session est ouverte sur
un autre compte y restent tant qu'il est sous son seuil.

`accounts list` et `cost report` sont des lectures : ils n'écrivent aucun
relevé de jauge (L71) ; le relevé revient aux exécuteurs, avant chaque tour,
ou à `ameesh cost gauges`. Un relevé dont la fenêtre est échue (`resets_at`
passé) compte pour **0 %** : un compte inutilisé dont le dernier relevé date
d'une fenêtre close n'est plus jugé au seuil (il ne servait pas, donc son
relevé n'était jamais rafraîchi). L'affichage garde le dernier relevé :
« codex-300min 0% (rythme 90%, remise à zéro passée, dernier relevé 93%) ».

Chaque choix de compte est journalisé avec sa raison dans le journal de
l'exécuteur (`[agent] compte codex : secondaire — codex-300min expire dans
52 min, 0 % utilisé (avant primaire : …)`, ou `— continuité : la session reste
sur primaire, sous son seuil`), une fois par changement de choix et non à
chaque sondage. Chaque changement du dernier choix est journalisé en base
(`account_switches`, type `bascule`, avec la même raison) et dans le fil de
l'équipe de l'agent qui l'a déclenché. Les retenues jusqu'à la remise à zéro
(`account_holds`) et les « retours au primaire » de 0027 §3 ne sont plus
posés : le choix par échéance les remplace. Une
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
