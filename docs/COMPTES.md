# Comptes multiples par fournisseur (L30)

Quand le compte actif d'un harnais atteint le seuil de la garde de budget
(`min(90 %, part écoulée + 10 points)`, décision 0019), les tours suivants
passent au compte suivant de la liste au lieu de mettre les agents en pause ;
ils reviennent au primaire dès que sa fenêtre est remise à zéro (décision
0027). Jamais au milieu d'un tour. Étude :
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

* L'ordre est la priorité : le premier est le primaire.
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
ameesh accounts list [--json]          # comptes, actif, état, jauges, dernières bascules
ameesh accounts use claude secondaire  # forçage manuel (plus de bascule automatique)
ameesh accounts auto [claude]          # retour en automatique
ameesh cost report                     # dépense par agent, compte actif, jauges par compte
ameesh cost gauges                     # historique des jauges, par compte
ameesh cost balance --record           # soldes, par compte de clé d'API
ameesh budget                          # plafonds du mesh en vigueur, source, pauses (L70)
```

Chaque bascule est journalisée en base (`account_switches`), dans le journal
de l'exécuteur et dans le fil de l'équipe de l'agent qui l'a déclenchée. Une
session reprise sous l'autre compte, ou tournée avec résumé, est dite dans le
fil. Tous les comptes au seuil : l'agent passe en pause (`blocked`, « tous
les comptes … au seuil »), comme avant L30.

Le compte d'origine de la session courante est enregistré
(`agent_registry.session_account`, L39) : l'exécuteur l'écrit avec l'id de
session, `ameesh adopt` l'écrit pour une session interactive adoptée (compte
sous lequel son fichier a été trouvé), et il s'efface avec la session. Le
marqueur du flux `events.jsonl` ne sert plus que de repli pour les sessions
antérieures. Quand ce compte est inutilisable (identifiants retirés, forfait
saturé) et que la session n'est pas portable, la rotation ne peut pas résumer
sous l'ancien compte : la session neuve s'ouvre sur un **brief de reprise
déterministe** (voir `ameesh resume` dans [EXPLOITATION.md](EXPLOITATION.md)).
