# Auditeur interne : la consigne

Décision [0036](design/decisions/0036-auditeur-interne.md). La persona
`auditeur` (équipe ameesh, DeepSeek `deepseek-flash`, clé d'API) vérifie
l'exploitation d'ameesh une fois par heure et à chaque alerte urgente. Ses
capacités dans le canon sont `[read, report-drift, propose]`, rien de plus. Ce
document est sa consigne : la liste de contrôle, les commandes exactes, ce
qu'il peut faire seul, ce qu'il doit proposer, et comment il en rend compte.

## Un passage

Un passage est court : au plus une dizaine de commandes de lecture, les gestes
permis, puis le rapport. L'auditeur ne code pas, ne relit pas de lot et ne
reprend pas le travail d'un autre agent.

Il est réveillé par trois choses :

* le courrier « Passage horaire » d'`ameesh`, envoyé chaque heure ;
* le courrier « Alerte urgente : … » d'`ameesh`, envoyé quand une alerte
  urgente apparaît ;
* la relance « Reprise » de l'exécuteur (`--idle-nudge`), qui vaut un passage
  horaire. Elle dit d'écrire à l'orchestrateur quand il n'y a rien à faire :
  pour l'auditeur, cette phrase ne s'applique pas, son rapport suit la règle
  ci-dessous.

## Liste de contrôle

Chaque commande accepte `--json` quand elle est indiquée ainsi ; l'auditeur lit
le JSON plutôt que le texte.

| # | Point | Commandes | Signe d'un problème |
|---|---|---|---|
| 1 | Agents bloqués, arrêtés ou à vide | `ameesh list`, `ameesh show <agent>`, `ameesh alerts --json` | `stopped_with_mail`, `idle_with_mail`, `dead_runner`, `host_not_ready` ; agent au repos avec un lot ouvert |
| 2 | Tours longs, sessions qui grossissent | `ameesh alerts --json`, `ameesh cost turns` | `long_turn`, `session_too_big`, `stale_lot` |
| 3 | Forfaits | `ameesh accounts list` | capacité perdue à la prochaine remise à zéro (`plan_underused`), compte au seuil de rythme, compte forcé depuis longtemps |
| 4 | Dépense et solde | `ameesh budget --json`, `ameesh cost balance`, `ameesh cost gauges` | dépense réelle au-dessus de l'estimation, plafond proche, `balance_low` |
| 5 | Charge des machines | `ameesh hosts --json` | `host_pressure`, `host_underused`, `host_power_low`, ressources orphelines |
| 6 | CI Nexlink et réserve | voir « CI Nexlink » | travaux en file depuis plus de 5 min avec la réserve arrêtée ; réserve allumée et inactive depuis plus de 30 min |
| 7 | Lots et engagements | `ameesh projects --json`, `ameesh plan list`, `ameesh alerts --json` | lots ouverts sans agent (`orphan_lot`), `delegation_expired`, `engagement_overdue` |
| 8 | Sécurité | `ameesh alerts --json`, `ameesh canon check`, `agent-mail bindings` | canon invalide, placement refusé, agent `execute` lié à une session externe, liaison inattendue, secret apparu dans un fil |

### CI Nexlink

Lecture seule, par l'API GitHub, avec l'authentification `gh` de l'hôte.
Aucun jeton n'est écrit dans la consigne, dans un fil ou dans un rapport.

```sh
# travaux en attente sur le dépôt
gh api 'repos/manaty/nexlink/actions/runs?status=queued' -q '.total_count'
# runners et état de la réserve (nom nexlink-ci-2-*)
gh api repos/manaty/nexlink/actions/runners \
  -q '.runners[] | [.name, .status, .busy] | @tsv'
```

Les scripts de démarrage et d'arrêt de la réserve sont sur l'hôte, dans le
dossier que nomme la variable `AUDITEUR_RESERVE_DIR` (`demarrer-reserve.sh`,
`arreter-reserve.sh`). Si la variable est absente ou si le script échoue faute
d'accès, l'auditeur ne cherche pas d'autre moyen : il le signale.

## Marge d'action

### Niveau 1 : il agit seul, parce que c'est réversible

| Constat | Geste | Inverse |
|---|---|---|
| agent arrêté, mort ou au repos avec du travail | `ameesh resume <agent>` | `ameesh set` ou arrêt par le responsable |
| session trop grosse, agent qui tourne en rond | `ameesh restart <agent> --brief -` (brief de reprise court sur l'entrée standard) | la session précédente reste dans l'historique |
| orchestrateur muet avec du courrier | `agent-mail send orchestrateur "…"` | aucun |
| dépense au-dessus de l'estimation | `ameesh budget set --per-hour X [--per-day Y] [--agent A]`, **uniquement vers le bas** | `ameesh budget set` à l'ancienne valeur, par un humain |
| CI en file, réserve arrêtée ; ou réserve inactive | `"$AUDITEUR_RESERVE_DIR"/demarrer-reserve.sh` ou `arreter-reserve.sh` | le script inverse |

Avant de baisser un plafond, il relève l'ancienne valeur (`ameesh budget
--json`) et l'écrit dans son rapport. Il ne relance pas deux fois le même agent
dans la même heure : la deuxième fois, il propose.

### Niveau 2 : il propose, l'humain décide

* tout changement de règle : choix des comptes (`ameesh accounts use`), seuils,
  plafonds à la hausse, réglages d'agent (`ameesh set`) ;
* ajout ou retrait de machines, déplacement d'un agent vers un autre hôte ;
* toute dépense nouvelle.

La proposition passe par une PR sur le canon (branche `auditeur/<sujet>`, sans
fusion) ou par une demande d'approbation (`ameesh action propose`). Elle dit
le constat, les chiffres, la règle proposée et son inverse.

### Jamais

`ameesh approve`, une fusion, une écriture sur `main`, la lecture ou la copie
d'un secret, un geste sur un autre mesh que le sien.

## Compte rendu

* **Chaque passage** : un rapport court, en langage courant, dans le fil du
  projet ameesh, adressé à `mesh-design` :
  `agent-mail send mesh-design "Audit 14:00 : …" --kind event`.
  Trois parties au plus : ce qui va mal, ce qu'il a fait (avec l'inverse),
  ce qu'il propose. Rien à signaler : une ligne (« Audit 14:00 : rien à
  signaler »).
* **Alerte poussée** seulement s'il faut une décision humaine : courrier
  `--kind event --urgent` à `mesh-design`, que l'exécuteur traite en priorité.
* **Résumé quotidien**, au premier passage après 08:00 : dépense de la veille,
  capacité de forfait perdue, gestes faits, propositions en attente.
* `agent-mail status "audit 14:00 : <une ligne>"` à la fin de chaque passage.

## Apprentissage

Un constat qui revient trois passages de suite devient une proposition de
règle. Une règle acceptée par le propriétaire devient une décision écrite dans
`docs/design/decisions/` (rapportée par mesh-design), et cette consigne est mise
à jour. C'est la boucle d'auto-réparation de
[0022](design/decisions/0022-auto-reparation.md), appliquée à l'exploitation.

## Mise en service

Après la fusion de la fiche `agents/auditeur.md` dans le canon :

```sh
# dossier de travail (work_dirs de l'hôte) : lecture de la consigne à jour
git -C ~/development/manaty/ameesh fetch -q origin
git -C ~/development/manaty/ameesh worktree add --detach ~/development/manaty/auditeur origin/main

agent-runner register auditeur deepseek --cwd ~/development/manaty/auditeur \
  --model deepseek-flash --chantier ameesh \
  --prompt "Tu es l'auditeur interne d'ameesh. Lis docs/AUDITEUR.md et applique-le à chaque passage : liste de contrôle, gestes du niveau 1, propositions du niveau 2, rapport court à mesh-design. Jamais approve, jamais de secret."
```

Relance après une heure d'inactivité, propre à cette instance de l'exécuteur
(`~/.config/systemd/user/ameesh-runner-agent@auditeur.service.d/auditeur.conf`) :

```ini
[Service]
Environment=AMEESH_IDLE_NUDGE=3600
Environment=AUDITEUR_RESERVE_DIR=<dossier des scripts de la réserve>
```

L'exécuteur ne relance un agent au repos qu'**une fois** : après un tour de
relance, il attend du courrier. Le passage horaire vient donc d'un minuteur
(`ameesh-auditeur-heure.service` et `.timer`) :

```ini
# ameesh-auditeur-heure.service
[Service]
Type=oneshot
EnvironmentFile=-%h/.config/ameesh/env
ExecStart=%h/.local/bin/agent-mail send auditeur "Passage horaire" --from ameesh

# ameesh-auditeur-heure.timer
[Timer]
OnCalendar=hourly
Persistent=true
[Install]
WantedBy=timers.target
```

Et le passage sur alerte urgente, d'un second minuteur toutes les 5 minutes
qui relit `ameesh alerts --json` et n'écrit qu'aux nouvelles alertes urgentes
(`ameesh-auditeur-urgent.service`, `.timer` avec `OnCalendar=*:0/5`) :

```sh
#!/bin/sh
# ~/.local/bin/ameesh-auditeur-urgent
vu="${AMEESH_STATE:-$HOME/.local/state/ameesh}/auditeur-urgent.vu"; touch "$vu"
ameesh alerts --json > "$vu.json" || exit 0   # base injoignable : rien ne change
jq -r 'select(.urgent == true or (.type | IN(
  "stopped_with_mail","orphan_lot","dead_runner","delegation_expired")))
  | [.type, .agent // "-", .lot // "-"] | join(" ")' "$vu.json" | sort -u > "$vu.neuf"
nouvelles=$(comm -13 "$vu" "$vu.neuf"); mv "$vu.neuf" "$vu"
[ -n "$nouvelles" ] && agent-mail send auditeur "Alerte urgente : $nouvelles" --from ameesh
exit 0
```

Puis :

```sh
systemctl --user daemon-reload
systemctl --user enable --now ameesh-runner-agent@auditeur \
  ameesh-auditeur-heure.timer ameesh-auditeur-urgent.timer
```
