# Bascule vers ameesh v1 — pas à pas, réversible

Ce plan fait passer le chantier des scripts v0 (`agent-mail.v0.py`,
`nexlink-agent.v0.sh`, boîtes fichiers) à ameesh v1, **sans jamais casser un
agent en cours**. Il remplace l'ancien plan du banc et part du brouillon
[`design/bascule-v1.md`](design/bascule-v1.md). Changements de fond :

* **plus de cérémonie de clé Ed25519 sur le PC** : l'autorité d'un humain se
  prouve par un reçu signé hors de portée des agents, sur son téléphone, par
  ameesh-approve ([0012](design/decisions/0012-autorite-par-ameesh-approve.md)).
  La clé Ed25519 d'agent reste pour la provenance (R13) et les tests, jamais
  comme autorité humaine ;
* un **canon d'amorçage** décrit membres, hôte, agents et placements
  (spec §4) ; `ameesh canon sync` est passé **sur chaque hôte avant toute
  réclamation** : sans état « ok » du canon, aucun agent du canon n'est
  réclamable ;
* les **orchestrateurs deviennent des agents à tours**, avec `ameesh attach`
  pour leur parler en direct
  ([0014](design/decisions/0014-orchestrateurs-a-tours-et-placement.md)).

Règles de la bascule :

* **un changement à la fois**, et **chaque étape a son retour arrière**,
  symétrique, écrit dans cette page ;
* **la v0 reste intacte** jusqu'à l'étape 8 : `~/.local/bin/agent-mail`,
  `nexlink-agent`, `~/.local/state/agent-mail` et
  `~/.local/state/nexlink-agents` ne sont ni modifiés ni supprimés ;
* **jamais deux boucles sur la même session** : la boucle v0 ne connaît pas le
  bail v1. Une boucle v0 est arrêtée, et **tout son groupe de processus est
  mort**, avant qu'un exécuteur v1 ne démarre pour cet agent (et inversement
  au retour arrière). Le fonctionnement en double est réservé aux agents
  factices ;
* **l'autorité se prouve** : aucune décision n'est reconnue sur son texte ;
* en cas de doute : `ameesh export-v0 --agents <nom>` remet le courrier dans la
  boîte fichier et la v0 reprend la main.

Actes réservés au propriétaire, marqués **[P]** : création de dépôts et
d'utilisateurs système, enrôlement de sa passkey, toute dépense, toute
exposition réseau.

---

## Prérequis — v1 terminée et essai de bout en bout vert

1. Tous les lots v1 fusionnés avec un OK de relecture (spec §13), dont L3
   (placement dans le prédicat de réclamation).
2. Sur le banc, l'essai de bout en bout et la démonstration passent, sur les
   deux pilotes :

   ```bash
   scripts/test.sh tests.test_bout_en_bout   # canon → exécuteur → fil → porte → reçu → réconciliation
   scripts/demo-v1.sh                        # le même scénario, commenté, base temporaire effacée
   ```

3. Les écarts d'intégration relevés par l'essai (lot L9) sont fermés (lot
   L9b) et l'essai passe **sans contournement** : `canon sync` remplit le
   registre des authentificateurs, `ameesh action request` / `fetch-receipt`
   parlent à ameesh-approve, les transitions d'actions sont dans le fil, le
   fil d'un agent du canon suit son équipe. `tests/banc_v1.py` ne joue plus
   que le téléphone (passkey LOGICIELLE de test — **jamais** en production)
   et la PR du canon.

**Retour arrière :** rien n'a changé.

---

## Étape 0 — sauvegardes et gel (15 min, aucun changement)

```bash
mkdir -p ~/backups
tar czf ~/backups/v0-$(date +%F-%H%M).tgz -C ~ \
    .local/state/agent-mail .config/agent-mail .local/state/nexlink-agents
sha256sum ~/.local/bin/agent-mail ~/.local/bin/nexlink-agent | tee ~/backups/v0-shas.txt
nexlink-agent list > ~/backups/v0-agents.txt     # agent, outil, état, session
```

Noter pour chaque agent son outil et sa session
(`~/.local/state/nexlink-agents/<nom>/{tool,session}`) : c'est la carte de la
bascule. Aucun agent ne doit être au milieu d'un tour long.

**Retour arrière :** rien n'a changé ; la sauvegarde sert à toutes les étapes.

---

## Étape 1 — base et paquet

**Postgres durable** (profil « local »,
[0016](design/decisions/0016-stockage.md)) : conteneur avec redémarrage
automatique et `pg_dump` quotidien copié hors du PC, ou paquet système. Le
conteneur du banc n'est pas une base de production.

```bash
python3 -m venv ~/.local/share/ameesh/venv
~/.local/share/ameesh/venv/bin/pip install -e ~/development/manaty/ameesh
for c in ameesh agent-runner ameesh-approve; do
  ln -s ~/.local/share/ameesh/venv/bin/$c ~/.local/bin/$c
done
# ~/.local/bin/agent-mail RESTE la v0 jusqu'à l'étape 8

install -m 700 -d ~/.config/ameesh
cat > ~/.config/ameesh/config.json <<'JSON'
{ "dsn": "postgresql://ameesh@127.0.0.1:5432/ameesh", "host": "pc-smichea",
  "project": "ameesh", "humans": ["smichea"] }
JSON
chmod 600 ~/.config/ameesh/config.json      # le mot de passe : ~/.pgpass (0600)
ameesh migrate
ameesh doctor --notify-test
ameesh import-v0 --dry-run                  # ce qui serait importé, sans rien toucher
```

Exercice de restauration obligatoire : `pg_dump`, restauration dans une base
jetable, `ameesh doctor` dessus, suppression.

**Retour arrière :** supprimer les liens de `~/.local/bin` (sauf
`agent-mail`, resté v0) ; arrêter la base. Rien ne dépend encore d'elle.

---

## Étape 2 — canon d'amorçage **[P] pour la création du dépôt**

Dépôt de canon du projet (`manaty/home` selon
[0005](design/decisions/0005-canon-okf.md), ou un dossier du dépôt du
projet), sur le modèle de [`examples/canon/`](../examples/canon/) :

* `federation.yaml` (rôles : responsable du projet = `human:smichea`) ;
* un `Member` par humain (`membres/smichea.md`, `authenticators: []` pour
  l'instant) ;
* un `Host` `pc-smichea` (responsable `human:smichea` ; politique : harnais
  claude, codex, deepseek ; modes `subscription` pour claude/codex, `api-key`
  pour deepseek ; **dossiers de travail** : `work_roots` par équipe, avec le
  gabarit `{agent}` pour un worktree par agent, et `work_dirs` pour les
  exceptions — voir [EXPLOITATION](EXPLOITATION.md#dossier-de-travail-des-agents-l31-l35)) ;
* un `Agent` (avec `responsible`, sans capacité `approve`) et un `Placement`
  (admission : `hosts: [pc-smichea]`, **sans** `cwd`) par agent : claude1–2, codex1–3,
  deepseek1–7 et les deux orchestrateurs. La liste et les outils viennent de
  la carte de l'étape 0. Un générateur `ameesh canon init --from-v0` n'existe
  pas encore : les fiches sont écrites à la main (ou par un agent, en
  brouillon) et le propriétaire relit la PR.

Puis, sur l'hôte :

```bash
git clone <dépôt du canon> ~/canon && echo "AMEESH_CANON=~/canon"   # ou "canon" dans config.json
ameesh canon check --fetch        # 0 erreur ; lu au commit fusionné d'origin/main
ameesh canon sync --fetch --bootstrap-ref main   # 1re fois seulement (voir ci-dessous)
ameesh list                       # agents du canon : hôte, responsable, aucun bail

# tous les agents tournent encore en v0 : les tenir hors de portée de tout
# exécuteur v1 (relance d'inactivité, courrier v1) jusqu'à leur propre bascule
for a in claude1 claude2 codex1 codex2 codex3 deepseek1 deepseek2 deepseek3 \
         deepseek4 deepseek5 deepseek6 deepseek7 <orchestrateurs>; do
  agent-runner stop "$a"
done
```

Aucun exécuteur ne tourne encore : rien ne démarre. `canon sync` n'écrit que
les colonnes déclaratives (harnais, hôte, dossier, responsable…), jamais bail,
session ni statut, et **ne lève jamais** un arrêt posé par `agent-runner stop`.

Registre des authentificateurs (spec §8.2) : la branche canonique qui fait foi
ne vient **jamais** du commit lu (son propre `federation.yaml` pourrait
s'autoriser). Elle vient de la configuration de l'hôte (`AMEESH_CANON_REF` /
`canon_ref`, posée par son responsable), sinon du manifeste du dernier commit
déjà appliqué (journal `authenticator_syncs` en base), sinon — premier
amorçage du registre — de `--bootstrap-ref <branche>` (journalisé, ignoré
ensuite) ; sans rien de cela, la synchronisation des authentificateurs est
refusée. `--ref` choisit ce qui est lu, jamais ce qui est canonique : le
commit lu doit être atteignable depuis la branche de suivi distant de
confiance, et descendre du dernier commit appliqué (jamais de retour en
arrière ; contrôle et écritures sont sérialisés par un verrou du registre).
Un changement de `ref` dans le manifeste ne prend effet qu'une fois fusionné
sur la branche canonique actuelle. Limite connue (spec §11) : un agent qui
partage l'utilisateur Unix d'ameesh peut réécrire `refs/remotes/…` du clone
(ou la configuration de l'hôte) ; seule la monotonie en base l'empêche alors
de revenir en arrière. Après une réécriture **légitime** de l'historique de la
branche canonique, tout est refusé (« ne descend pas du dernier commit
appliqué ») jusqu'à ce que le propriétaire vide le journal
(`DELETE FROM authenticator_syncs`) et réamorce avec `--bootstrap-ref`.

**Retour arrière :** retirer `AMEESH_CANON` (ou `canon` de la configuration).
Le registre garde ses colonnes et ses arrêts, sans effet tant qu'aucun
exécuteur ne tourne.

**Plusieurs canons** (manaty + Acme, L42) : `canons` dans
`config.json` ou `AMEESH_CANONS` — voir
[EXPLOITATION](EXPLOITATION.md#plusieurs-canons-l42-decision-0031). Le premier
reste le canon par défaut ; un hôte à un seul canon ne change rien. Depuis
L44 (migration 0035), chaque canon synchronise les passkeys de ses humains
dans son propre registre : donner une `ref` à chaque entrée de `canons`, ou
amorcer une fois chaque canon suivant (`ameesh canon sync --canon <id>
--bootstrap-ref main`, journalisé) ; puis relancer `deploy/sql/role-approve.sql`
(ameesh-approve lit `actions.canon` et `authenticators.canon`).

---

## Étape 3 — ameesh-approve **[P]**

1. **Utilisateur système dédié** `ameesh-approve`, distinct de celui des
   agents ; son dossier d'état en `0700`, son jeton de service en `0600` :

   ```bash
   sudo useradd --system --create-home ameesh-approve
   sudo -u ameesh-approve ameesh-approve gen-token
   ```

   ameesh, côté agents, lit une copie de ce jeton : il permet de **demander**
   une approbation, jamais d'en signer une. Copie en `0600` à l'utilisateur
   des agents (refusée sinon), puis dans `~/.config/ameesh/env` :
   `AMEESH_APPROVE_URL=http://127.0.0.1:8765` (https, ou http sur la boucle
   locale seulement) et `AMEESH_APPROVE_TOKEN_FILE=<copie>`. ameesh ne
   l'affiche ni ne l'écrit jamais (ni fil, ni journal), ne passe par aucun
   mandataire et ne suit aucune redirection.
2. **Exposition au téléphone — une page Nexlink**
   ([0017](design/decisions/0017-approbation-via-page-nexlink.md)). Le service
   n'écoute que sur la boucle locale (`--bind 127.0.0.1 --port 8765`) ; la page
   Nexlink le relaie en HTTPS. Le RP ID est le domaine de la page, l'origine
   `https://<domaine>` (`AMEESH_APPROVE_RP_ID`, `AMEESH_APPROVE_ORIGINS`, les
   mêmes côté ameesh pour vérifier les reçus). **La mise en ligne est un acte du
   propriétaire**, après le OK de revue de sécurité de L7 ; un changement de
   domaine oblige à réenrôler les passkeys.
3. **Enrôlement de la passkey du propriétaire, par le canon** :

   ```bash
   sudo -u ameesh-approve ameesh-approve enroll-link --approver human:smichea
   #   → lien à usage unique, à ouvrir SUR LE TÉLÉPHONE ; la passkey créée devient
   #     une PROPOSITION (fichier sous <état>/proposals/), rien n'est actif
   ```

   Le propriétaire vérifie l'empreinte hors bande, reporte l'entrée proposée
   dans `authenticators` de `membres/smichea.md` **par PR**, la fusionne, puis
   `ameesh canon sync --fetch` sur chaque hôte : il recopie
   `Member.authenticators` dans le registre de confiance (spec §8.2), depuis le
   commit fusionné de la branche canonique seulement ; `ameesh authenticator
   list` montre la passkey active, avec le commit du canon. Un hôte dont le
   clone est en retard sur le registre (retrait déjà appliqué par un autre
   hôte) refuse d'y écrire (`authentificateurs : REFUSÉ … --fetch`) : il ne
   réactive jamais une passkey retirée.
4. **Essai réel** : une action `shell-noop` de classe `irreversible` reste
   bloquée sans reçu (`ameesh action execute` → `[receipt_required]`) ;
   `ameesh action request <id> --approver human:smichea` dépose la demande et
   écrit le lien dans le fil ; une fois signé sur le téléphone,
   `ameesh action fetch-receipt <id>` récupère le reçu, le vérifie et
   l'attache ; l'exécution passe, et le rejeu du reçu échoue
   (`ameesh receipt verify` → `[replay]`).

**Retour arrière :** arrêter le service (`systemctl stop ameesh-approve`) et
retirer la page Nexlink. Les actions irréversibles restent bloquées — le
comportement sûr. Retirer une passkey : PR qui la supprime du canon, puis
`canon sync`.

---

## Étape 4 — un agent pilote (`deepseek7`)

Dans cet ordre, sans raccourci :

1. attendre la fin de son tour en cours (`nexlink-agent list`, fenêtre de
   l'agent) ;
2. arrêter la boucle v0 et **attendre la mort de tout son groupe** :

   ```bash
   PID=$(cat ~/.local/state/nexlink-agents/deepseek7/pid)
   PGID=$(ps -o pgid= -p "$PID" | tr -d ' ')
   SESSION=$(cat ~/.local/state/nexlink-agents/deepseek7/session)
   nexlink-agent stop deepseek7
   while pgrep -g "$PGID" >/dev/null || pgrep -f -- "$SESSION" >/dev/null; do sleep 2; done
   ```

   Si un harnais survit au-delà d'un tour, `kill -TERM -- -"$PGID"` puis
   `kill -KILL -- -"$PGID"` : aucun processus ne doit encore tenir la session ;
3. reprendre courrier et session sur cet état stable, puis lever l'arrêt posé
   à l'étape 2 (la consigne remet l'agent en file, sur la session de la v0) :

   ```bash
   ameesh import-v0 --agents deepseek7
   agent-runner register deepseek7 deepseek --session "$SESSION" \
       --prompt "Reprise sous ameesh v1 : continue ton lot en cours."
   ameesh canon sync              # rétablit les colonnes du canon ; état « ok » requis
   ameesh show deepseek7          # session = $SESSION, statut queued, aucun bail
   ```

4. démarrer l'exécuteur v1 pour ce seul agent, puis un aller-retour :

   ```bash
   agent-runner --agents deepseek7 --poll 5
   #   → « bail acquis : deepseek7 », « LISTEN agent_mail, agent_lease »
   ameesh mail send deepseek7 "aller-retour de bascule" --from smichea
   ameesh fil show ameesh         # le message est dans le fil, en clair
   ameesh list                    # bail, non-lus, budget
   ```

   **Hooks** : la ligne de commande des hooks ne change pas
   (`agent-mail hook <harnais>`). Un agent lancé par l'exécuteur reçoit
   `AGENT_MAIL_NAME`, `AMEESH_RUNNER_ID` et `AMEESH_LEASE_EPOCH` : le hook ne
   livre que si le bail est vivant et détenu par cet exécuteur. Le dossier ne
   donne jamais d'identité. Une session externe n'en a une que si elle est
   liée (`ameesh mail bind`, L41) : voir
   [Passer les hooks à la v1](#passer-les-hooks-à-la-v1-avance-de-létape-8-pour-les-hooks-décision-0030).

   **Courrier pendant la coexistence (jusqu'à l'étape 8)** : la commande
   `agent-mail` installée (`~/.local/bin/agent-mail`, celle qu'appellent les
   hooks par leur chemin absolu) est encore la v0 : `agent-mail inbox` y lit la
   boîte **fichier**, jamais la base. L'exécuteur v1 ne renvoie donc plus
   l'agent vers `agent-mail inbox` : un tour déclenché par du courrier, des
   événements (regroupés) ou un message prioritaire porte le **contenu** des
   messages v1 dans sa consigne (expéditeur, horodatage, corps lisible comme
   dans le fil, rappel qu'un message d'agent n'a jamais l'autorité du
   propriétaire). Plafond sur la consigne **entière**, en octets UTF-8
   (`AMEESH_PROMPT_MAIL_MAX`, 20 000 par défaut, résumé de reprise compris) :
   les plus anciens d'abord, le nombre restant est indiqué, et ces
   messages-là restent non livrés pour le tour suivant.

   Une seule règle de remise, pour l'exécuteur comme pour le hook v1 :
   **réserver** sous un jeton (la ligne de l'agent est verrouillée dans le
   registre et le bail `owner`/`epoch` contrôlé dans la même instruction ; un
   message déjà réservé ne l'est jamais deux fois), **montrer**, puis
   **solder** cette réservation-là seulement, bail toujours vivant. L'exécuteur
   solde juste après le lancement du harnais et annule si le tour ne démarre
   pas (harnais absent, dossier absent, échec du lancement) ; le hook ne voit
   donc jamais ce qui est dans la consigne, et livre ce qui arrive pendant le
   tour. Bail perdu entre réservation et remise : rien n'est soldé, la remise
   est signalée « incertaine » (journal et fil) ; panne : la réservation expire
   ou change d'epoch, et le message est remis de nouveau, marqué « re-livré »
   — un doublon signalé, jamais une perte.

   L'agent **répond par `agent-mail send` (v0)** jusqu'à l'étape 8 : la
   réponse part dans la boîte fichier v0 et l'opérateur la voit comme avant.
   Pour vérifier qu'un message v1 a bien été vu : `ameesh fil show <projet>`
   et le journal de l'exécuteur (le tour « messages » cite la consigne), plutôt
   que `agent-mail inbox`.

**Retour arrière, symétrique :**

```bash
# 1. plus aucun tour v1 pour cet agent, puis arrêt de son exécuteur (Ctrl-C ou
#    SIGTERM : bail rendu, harnais tué avec son groupe) ; vérifier qu'il ne reste rien
agent-runner stop deepseek7
pkill -TERM -f "ameesh.runner.*--agents deepseek7|agent-runner --agents deepseek7"
ameesh show deepseek7                       # bail : —
while pgrep -f -- "$SESSION" >/dev/null; do sleep 2; done
# 2. le courrier retourne dans la boîte fichier
ameesh export-v0 --agents deepseek7
# 3. la boucle v0 reprend la MÊME session
nexlink-agent start deepseek7 deepseek "<consigne>" "$SESSION"
```

Les messages arrivés entre-temps en base sont réécrits dans la boîte v0 ; aucun
n'est perdu.

---

## Étape 5 — les autres agents, par vagues de 1 ou 2

Même procédure que l'étape 4, agent par agent : fin du tour, arrêt v0 et mort
du groupe, `import-v0`, `agent-runner register … --session … --prompt …` (qui
lève l'arrêt de l'étape 2), `ameesh canon sync`. Dès que deux agents ou plus
sont basculés, un seul exécuteur par hôte les sert (`agent-runner` sans
`--agents`) : les agents encore en v0 restent arrêtés au registre, il ne les
réclame pas.

Après chaque vague :

```bash
ameesh list                 # aucun agent basculé sans bail ; aucun agent v0 avec bail
ameesh canon check          # canon valide, état « ok » enregistré pour l'hôte
ameesh fil list             # les fils avancent
ameesh doctor --notify-test
```

**Retour arrière (par agent), symétrique :** `agent-runner stop <nom>` (plus
de nouveau tour ; `canon sync` ne lève jamais cet arrêt), attendre que son bail
soit rendu et que son groupe soit mort (`ameesh show <nom>`, `pgrep -f` sur sa
session), `ameesh export-v0 --agents <nom>`, puis `nexlink-agent start` sur la
même session. Pour le rebasculer plus tard : la procédure de l'étape 4.

---

## Étape 6 — les orchestrateurs, agents à tours

Pour chaque orchestrateur (déclaré au canon comme les autres agents) :

1. le **service de surveillance** qui nourrissait le `Monitor` de la session
   dépose désormais ses constats dans la boîte de l'orchestrateur, en
   événements : `ameesh mail send <orchestrateur> "<constat lisible>" --kind
   event [--urgent] --from surveillance`. Les événements sont regroupés (au plus
   un réveil par `AMEESH_EVENT_COALESCE` secondes, 120 par défaut) sauf
   `--urgent` ;
2. fin de la session interactive (fin de tour, sortie du harnais, **mort du
   groupe** comme à l'étape 4), `import-v0`, `agent-runner register …
   --session … --prompt …`, `ameesh canon sync` : l'exécuteur le réclame et le
   réveille par des tours (messages, événements regroupés), sur la même
   session ;
3. pour lui parler en direct : `ameesh attach <orchestrateur>` (refusé pendant
   un tour, sauf `--wait`). Attach prend le bail sous `attach:<utilisateur>@<hôte>`,
   suspend la réclamation automatique, lance le harnais en interactif sur la
   même session ; à la sortie, le bail revient à l'exécuteur.

**Retour arrière, symétrique :** `agent-runner stop <orchestrateur>`, bail
rendu et groupe mort ; arrêter le dépôt d'événements par la surveillance ;
`ameesh export-v0 --agents <orchestrateur>` ; relancer la session interactive
sur la même session, avec la surveillance en `Monitor` dans la session.

---

## Étape 7 — supervision (systemd)

**L'exécuteur**, service utilisateur ; le canon est synchronisé avant toute
réclamation (un canon illisible ou invalide n'empêche pas le démarrage : il
ferme la réclamation des agents du canon, fail closed) :

```ini
# ~/.config/systemd/user/agent-runner.service
[Unit]
Description=ameesh — exécuteur de l'hôte (un par machine)
After=network-online.target

[Service]
# L106 : PATH explicite — l'unité peut démarrer avant que la session n'importe
# le sien (shims et installations mise, node pour dsh)
Environment=PATH=%h/.local/share/mise/shims:%h/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/bin:/bin
EnvironmentFile=%h/.config/ameesh/env        # AMEESH_HOST, AMEESH_CANON, AMEESH_APPROVE_* (PATH=… possible)
ExecStartPre=-%h/.local/share/ameesh/venv/bin/ameesh canon sync --fetch
ExecStart=%h/.local/share/ameesh/venv/bin/agent-runner --poll 5
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload && systemctl --user enable --now agent-runner
journalctl --user -u agent-runner -f
ameesh doctor --harness     # L106 : binaires résolus (chemin, provenance), unités manquantes
```

**Un exécuteur par agent** (la forme retenue sur le poste) : le modèle
`deploy/systemd/ameesh-runner-agent@.service` (même PATH explicite), une
instance par agent mené — **y compris un agent tenu en `ameesh attach`** :
si la session interactive meurt, c'est son exécuteur qui reprend la même
session (L106). `ameesh doctor` signale un agent mené sans unité activée.

```bash
install -m 0644 'deploy/systemd/ameesh-runner-agent@.service' ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now ameesh-runner-agent@<agent>
```

Après chaque PR fusionnée au canon : `ameesh canon sync --fetch` sur chaque
hôte (à la main ou par un minuteur).

**Le service de surveillance** des orchestrateurs, service utilisateur à côté
de l'exécuteur.

**Les alertes poussées** (L38) : `ameesh notify`, service utilisateur à côté
de l'exécuteur — unité d'exemple `deploy/systemd/ameesh-notify.service`,
configuration et secrets dans [EXPLOITATION.md](EXPLOITATION.md#alertes-poussées--ameesh-notify).

**ameesh-approve [P]**, service système sous son utilisateur, jamais celui des
agents :

```ini
# /etc/systemd/system/ameesh-approve.service
[Unit]
Description=ameesh-approve — approbations humaines (passkeys)
After=network-online.target

[Service]
User=ameesh-approve
EnvironmentFile=/etc/ameesh-approve/env       # AMEESH_DSN, AMEESH_APPROVE_RP_ID, _ORIGINS, _PUBLIC_URL
ExecStart=/opt/ameesh/venv/bin/ameesh-approve serve --bind 127.0.0.1 --port 8765
Restart=on-failure
NoNewPrivileges=yes
ProtectHome=read-only

[Install]
WantedBy=multi-user.target
```

**Base distante (VPN, WireGuard)** : depuis L72, l'exécuteur traverse une
panne passagère de la base sans s'arrêter (voir
[EXPLOITATION.md](EXPLOITATION.md#panne-passagère-de-la-base-l72)). Deux
réglages du DSN complètent : des *keepalives* TCP pour qu'une connexion morte
(LISTEN, pilote psycopg) soit détectée en une minute plutôt qu'au délai TCP du
noyau (un quart d'heure et plus), et un délai de connexion adapté au lien :

```bash
# ~/.config/ameesh/env (ou AMEESH_DSN de l'unité)
AMEESH_DSN='postgresql://ameesh@10.77.0.1:5432/ameesh?keepalives=1&keepalives_idle=30&keepalives_interval=10&keepalives_count=3&tcp_user_timeout=60000'
AMEESH_CONNECT_TIMEOUT=10   # défaut depuis L72 (3 s avant)
```

Côté serveur, `tcp_keepalives_idle = 60` dans `postgresql.conf` libère de même
les sessions d'un client disparu.

À surveiller : agents basculés sans bail vivant, non-lus de plus de 15 min,
baux expirés, `ameesh decisions` (approbations en attente, issues inconnues à
trancher), état du canon par hôte (`ameesh canon check`).

**Retour arrière :** `systemctl --user disable --now agent-runner` (baux rendus,
harnais tués avec leur groupe), puis, agent par agent, le retour arrière de
l'étape 4 ; `systemctl disable --now ameesh-approve`.

---

## Étape 8 — fin de vie de la v0 (après une à deux semaines)

* `~/.local/bin/agent-mail` pointe enfin vers la v1 (lien vers le venv) ;
* garder `nexlink-agent`, `nexlink-agents` et l'état fichier **en lecture
  seule** un mois de plus ;
* `nexlink-agents` (relance après redémarrage) est remplacé par les services
  systemd ;
* archiver la sauvegarde de l'étape 0 avec la date de bascule ; ne supprimer
  les scripts v0 qu'après une restauration testée.

**Retour arrière :** réinstaller les scripts depuis la sauvegarde, remettre
`~/.local/bin/agent-mail` sur la v0, `ameesh export-v0` pour le courrier, puis
les boucles v0 (étape 4, retour arrière).

---

## Passer les hooks à la v1 (avance de l'étape 8 pour les hooks, décision 0030)

**Geste du propriétaire [P].** Cette section remplace une commande de
`~/.local/bin` et modifie les configurations des harnais
(`~/.claude*/settings.json`, `~/.codex*/hooks.json`). Aucun agent ne le fait
à sa place : un agent peut préparer les commandes, mais c'est le propriétaire
qui les lance et qui vérifie. Elle avance l'étape 8 **pour les hooks
seulement** (décision 0030 « Pas de travail sans réveil possible », point 6,
lot L41) : `nexlink-agent` et l'état fichier v0 restent en place.

### Constat

* Les hooks du poste appellent `~/.local/bin/agent-mail`, qui est encore la
  **v0**, directement ou par le pont local `~/.local/bin/ameesh-session-mail-hook`
  (configurations Claude secondaire et tertiaire), qui pose `AGENT_MAIL_NAME`
  puis appelle la v0.
* La v0 **tire l'identité du dossier** (`AGENT_MAIL_NAME`, sinon alias de
  `~/.config/agent-mail/aliases.tsv` par préfixe, sinon nom du dossier) : une
  session sous `~/Work` prenait l'identité `orchestrateur` d'une autre équipe.
* La v0 lit la boîte **fichier** : une session externe ne voit jamais son
  courrier v1 (Postgres). Même liée par le pont, elle ne reçoit que la boîte
  fichier.
* La v1 (`agent-mail hook <harnais>`, L41) ne tire jamais l'identité du
  dossier : `AGENT_MAIL_NAME` (et le bail si l'exécuteur l'a posé), sinon la
  **liaison de session** (`ameesh mail bind`, source `session`) retrouvée par
  l'identifiant de session que le harnais passe au hook, avec contrôle du PID
  ancêtre s'il est lié. Sans l'un ni l'autre : **rien n'est remis, rien n'est
  écrit**, sortie 0.

### Avant de commencer

1. ameesh installé avec L41 dans le venv, schéma migré : `ameesh migrate`
   (applique `0034_liaisons_de_session`), puis `ameesh mail bindings` répond
   « aucune liaison de session ».
2. **Plus aucun agent sous boucle v0** (`nexlink-agent list` : aucun agent
   actif) : après le remplacement, `agent-mail send` et `agent-mail inbox`
   parlent à la base, plus à la boîte fichier. Un agent encore mené par la v0
   le serait sans courrier : le rendre d'abord à l'exécuteur (étapes 4 à 6),
   ou reporter cette section.
3. Courrier v0 en attente pour les sessions externes : `ameesh import-v0
   --dry-run`, puis `ameesh import-v0 --agents <noms>` pour le passer en base.
4. Noter, pour chaque session interactive à garder joignable, son harnais, son
   identifiant de session et le PID du harnais (`ps -o pid,args -C claude`,
   `-C codex`). L'identifiant est celui que le harnais passe au hook :
   Claude le montre dans `/status` et dans le nom du transcript
   (`~/.claude*/projects/…/<id>.jsonl`), Codex dans le nom du fichier de
   session (`~/.codex*/sessions/…/rollout-…-<id>.jsonl`).

### Sauvegarde

```bash
mkdir -p ~/backups/hooks-v1-$(date +%F-%H%M) && cd "$_"
cp -a ~/.local/bin/agent-mail agent-mail.v0
cp -a ~/.local/bin/ameesh-session-mail-hook .
cp -a ~/.config/ameesh/external-session-bindings.json .
for f in ~/.claude*/settings.json ~/.codex*/hooks.json; do
  cp -a "$f" "$(echo "${f#$HOME/}" | tr / _)"
done
sha256sum * > SHA256SUMS
```

### Bascule

L'ordre compte (L46). Le pont local pose `AGENT_MAIL_NAME` (source
`explicit`) : tant qu'il appelle `agent-mail`, une v1 derrière lui
remettrait à la session externe le courrier d'un agent nommé dans le pont —
y compris d'un agent que l'exécuteur mène déjà (`coordinateur`), et
écrirait sur sa ligne. Le pont est donc **retiré d'abord**, pendant que
`agent-mail` est encore la v0 (qui ne lit que la boîte fichier et ne touche
pas à la base) ; la v1 n'arrive qu'ensuite. Depuis L46, la v1 refuse de
toute façon la remise `explicit` à un agent qui détient un bail vivant, et
un hook sans bail ne change plus que `last_seen` sur la ligne d'un agent
`execute` ; l'ordre ci-dessous reste la règle (il protège aussi un agent
entre deux tours, sans bail vivant à l'instant).

1. **Remplacer, dans les configurations des harnais, les hooks qui appellent
   le pont** par l'appel direct à `agent-mail` (mêmes événements, même
   harnais) :

   ```text
   /home/smichea/.local/bin/ameesh-session-mail-hook claude   →   /home/smichea/.local/bin/agent-mail hook claude
   ```

   (`~/.claude-secondary/settings.json`, `~/.claude-tertiary/settings.json`.)
   Jusqu'au point 3, ces hooks appellent encore la v0, comme ceux de
   `~/.claude`, `~/.codex` et `~/.codex-secondary` aujourd'hui : rien n'est lu
   en base. Le pont `ameesh-session-mail-hook` et son fichier restent en
   place, inutilisés, jusqu'à la fin de vie de la v0 (étape 8).

   ```bash
   grep -c ameesh-session-mail-hook ~/.claude*/settings.json ~/.codex*/hooks.json   # 0 partout
   ```

2. **Importer les liaisons du pont** (inertes pour la v0 ; le dossier `cwd`
   du pont n'est pas repris : il ne donne jamais d'identité) :

   ```bash
   ameesh mail bind --import ~/.config/ameesh/external-session-bindings.json
   ameesh mail bindings
   ```

   Le rapport dit, entrée par entrée, ce qui est importé et ce qui est ignoré
   avec la raison : agent `execute` (mené par l'exécuteur, qui lui remet son
   courrier : c'est le cas attendu d'un agent comme `coordinateur` après sa reprise par
   l'exécuteur — L46 : l'import ne force jamais), agent qui détient un bail
   vivant, session déjà liée à un autre agent, harnais ou nom invalide. Un
   agent encore inconnu du registre y est inscrit comme **externe**. Une
   session reprise depuis l'écriture du pont a un **nouveau PID** : la
   relier avec le PID courant, sinon le hook la refusera (« pid … absent de
   l'ascendance ») :

   ```bash
   ameesh mail bind <agent> --session <id> --harness claude|codex|deepseek --pid <PID du harnais>
   ```

   `--pid` est recommandé : sans lui, quiconque connaît l'identifiant de
   session sur cet hôte reçoit le courrier de l'agent. Un agent `execute` est
   refusé (« agent mené par l'exécuteur ; une session externe lui volerait son
   courrier ») : le reclasser d'abord (`ameesh set <agent> mode=externe`) s'il
   est en réalité une session humaine ; `--force` n'est qu'un dernier recours.

3. **Remplacer `agent-mail` par la v1** (le lien vers le venv, comme les
   autres commandes de l'étape 1) — en dernier, une fois le pont retiré des
   configurations :

   ```bash
   ln -sfn ~/.local/share/ameesh/venv/bin/agent-mail ~/.local/bin/agent-mail
   agent-mail --help | head -3          # « agent-mail (mesh v1) »
   ```

   Les hooks qui appellent `agent-mail hook <harnais>` et la barre d'état
   (`agent-mail statusline`) passent à la v1 par ce lien.

### Vérification

```bash
grep -c ameesh-session-mail-hook ~/.claude*/settings.json ~/.codex*/hooks.json   # 0 partout
ameesh mail bindings
```

* **Dans une session liée** (demander à l'agent de lancer la commande, ou
  depuis un shell lancé par la session) : `agent-mail whoami` affiche
  `<agent> [session] liaison <harnais> <id> sur <hôte>, pid <N>, par <qui>`.
  Puis `ameesh mail send <agent> "essai de remise v1" --from smichea`
  depuis un terminal : le message apparaît au prochain prompt de la session,
  et `ameesh fil show <projet>` le montre remis.
* **Dans une session non liée** : `agent-mail whoami` répond « identité non
  liée » (code 1) ; un message envoyé à un agent dont le dossier porterait le
  nom **n'apparaît pas** dans la session, et reste non lu en base
  (`ameesh mail inbox <agent>`).
* Un agent de l'exécuteur : son tour suivant se passe comme avant
  (`ameesh list`, journal de l'exécuteur), son hook remet sous son bail.

### Retour arrière, symétrique

1. Remettre la v0 : `cp -a ~/backups/hooks-v1-<date>/agent-mail.v0
   ~/.local/bin/agent-mail` (le fichier, pas un lien), puis `agent-mail
   --help | head -3` (« boîte aux lettres locale »).
2. Remettre les configurations des harnais depuis la sauvegarde (les hooks
   des configurations Claude secondaire et tertiaire rappellent
   `ameesh-session-mail-hook`), et vérifier les empreintes
   (`sha256sum -c SHA256SUMS` dans le dossier de sauvegarde, pour les
   fichiers remis).
3. Courrier v1 arrivé entre-temps pour les sessions externes :
   `ameesh export-v0 --agents <noms>` le réécrit dans la boîte fichier, que la
   v0 relit.
4. Les liaisons en base sont inertes pour la v0 ; les révoquer si on renonce
   à la bascule : `ameesh mail unbind --session <id> --harness <h>` pour
   chacune (`ameesh mail bindings`).

---

## Mise à jour d'ameesh vers L35 — dossiers de travail

Depuis L31 (v1.3.0), le dossier de travail vient de la fiche `Host`, plus du
`cwd` des fiches `Placement`. Un hôte qui n'avait rien déclaré a vu ses agents
recevoir un `cwd` vide et rester bloqués (« dossier de travail absent :
None »). L35 ajoute `policy.work_dirs`, le gabarit `{agent}` et un repli
transitoire sur l'ancien `cwd`.

**Ce que v1.3.0 fait des réglages L35** (recette locale ci-dessous) :
`canon check` 1.3.0 ne dit **rien** ; `work_dirs` est ignoré ; `{agent}` dans
`work_roots` est recopié **littéralement** (`/wt/acme-{agent}` pour tous les
agents de l'équipe). Un `canon check` 1.3.0 ne valide donc jamais un canon
L35, et un exécuteur 1.3.0 encore actif (sync au démarrage et toutes les
`AMEESH_CANON_SYNC_INTERVAL`, 300 s) écrirait ces chemins faux au registre.
D'où l'ordre : **arrêter l'ancien, installer L35, valider avec L35, puis
seulement publier**.

1. **Préparer sans publier.** Écrire les réglages sur une branche du canon
   (PR **non fusionnée**) : `policy.work_roots` par équipe, avec `{agent}`
   pour un worktree par agent (ex. `nexlink:
   "~/development/manaty/nexlink-{agent}"`), et `policy.work_dirs: {<agent>:
   <chemin>}` pour les exceptions. Recette locale avec le binaire L35, hors
   de l'installation de l'hôte et sans base :

   ```bash
   python3 -m venv /tmp/ameesh-l35 && /tmp/ameesh-l35/bin/pip install -q <dépôt ameesh au gel L35>
   git clone -q <url du dépôt du canon> /tmp/canon-l35     # copie jetable, ~/canon intact
   AMEESH_CONFIG=/tmp/aucune-config.json /tmp/ameesh-l35/bin/ameesh canon check \
       --canon /tmp/canon-l35 --ref origin/<branche-PR> --host pc-smichea
   #   attendu : 0 erreur, aucun host-policy-invalid, admission-cwd-inherited
   #   ni host-work-dir-missing
   ```

2. **Arrêter l'ancien exécuteur, et avec lui son sync périodique** :
   `systemctl --user stop agent-runner` (baux rendus, tours en cours tués avec
   leur groupe). Ne lancer **aucun** `ameesh canon sync` 1.3.0 à partir d'ici.
3. **Installer L35** (dépôt installé en éditable : `git -C
   ~/development/manaty/ameesh checkout <gel L35>`), puis `ameesh --version`
   et `ameesh canon check --fetch` avec CE binaire sur le canon actuel
   (encore sans les nouveaux réglages) : le repli `admission-cwd-inherited`
   y apparaît là où les anciens `cwd` servent encore.
4. **Publier** : fusionner la PR du canon, puis `ameesh canon check --fetch`
   (binaire L35) : 0 erreur, aucun `admission-cwd-inherited` ni
   `host-work-dir-missing`.
5. `ameesh canon sync --fetch` : aucune ligne « cwd hérité » ni « aucun
   dossier de travail » ; `ameesh list --json` montre un `cwd` distinct et
   existant pour chaque agent.
6. `systemctl --user start agent-runner`. Un agent resté `blocked`
   (« dossier absent ») repart seul dès que le `cwd` du registre est bon.
7. Plus tard, retirer le `cwd` des fiches `Placement` (ignoré :
   `admission-cwd-ignored`).

**Retour arrière vers v1.3.0 — le paquet seul ne suffit pas.** Un canon L35
lu par 1.3.0 donne des chemins littéraux (`…-{agent}`) et ignore `work_dirs`.
Dans l'ordre :

1. `systemctl --user stop agent-runner` (binaire L35).
2. **Rendre le canon compatible 1.3.0 avant tout sync** : PR qui retire
   `work_dirs` et tout `{agent}`, et ne garde que `work_roots: {<équipe>:
   <chemin>}` / `work_root` ; `canon check` du binaire **L35** sur cette
   branche (il valide un sous-ensemble que 1.3.0 lit à l'identique), puis
   fusion.
3. **Limite de 1.3.0** : un seul dossier par équipe et par hôte
   (`work_roots[équipe]` ou `work_root/<équipe>`) ; le `cwd` des fiches
   `Placement` est ignoré. Plusieurs agents d'une même équipe avec des
   worktrees distincts (cas nexlink) **ne peuvent pas** tourner chacun dans le
   sien en 1.3.0. Pour chacun de ces agents, avant de relancer : soit
   l'arrêter (`agent-runner stop <agent>`), soit accepter le dossier commun de
   l'équipe s'il n'y a qu'un agent actif à la fois, soit le rendre à la v0
   (`ameesh export-v0 --agents <agent>`, étape 4).
4. Réinstaller 1.3.0 (`git -C ~/development/manaty/ameesh checkout v1.3.0`),
   `ameesh canon check --fetch`, `ameesh canon sync --fetch`, vérifier
   `ameesh list --json` (aucun `cwd` contenant `{`, aucun agent actif sans
   dossier), puis `systemctl --user start agent-runner`.

**Recette locale de compatibilité** (faite pour L35 sur le canon d'exemple,
sans base ni exécuteur, en lisant le même canon avec les deux versions) :

| Canon d'essai (`atelier`) | L35 : orchestre / relecteur | 1.3.0 : orchestre / relecteur | `canon check` 1.3.0 |
|---|---|---|---|
| `work_roots: {acme-web: '/wt/acme-{agent}'}`, `work_dirs: {relecteur: /wt/relecture}` | `/wt/acme-orchestre` / `/wt/relecture` | `/wt/acme-{agent}` / `/wt/acme-{agent}` | aucun constat |
| `work_roots: {acme-web: /wt/acme-web}` | `/wt/acme-web` / `/wt/acme-web` | `/wt/acme-web` / `/wt/acme-web` | aucun constat |

---

## Mise à jour vers L36–L46 — décisions 0030 et 0031

Les lots L36 à L46 apportent les migrations **0030 à 0036** : mode d'agent et
raison d'arrêt (0030), délégation à échéance (0031), plusieurs canons (0032),
compte de la session (0033), liaisons de session (0034), authentificateurs par
canon (0035), puis les corrections de relecture de L46 (0036 : `actions.canon`
immuable hors rebase du canon par défaut, heure de démarrage du PID lié). Les
gestes marqués [P] sont ceux du propriétaire.

**Ce qui casse l'ancien code — tout se met à jour ENSEMBLE.**

* **0032** : la clé de `canon_state` devient `(host, canon)`. L'ancien code
  écrit l'état du canon par `ON CONFLICT (host)` : après la migration, chacune
  de ses synchronisations échoue (plus de contrainte unique sur `host` seul).
* **0035** : l'index unique des authentificateurs devient `(canon, facade,
  credential_id)`. L'ancien code inscrit par `ON CONFLICT (facade,
  credential_id)` : sa synchronisation des passkeys échoue.
* **0036** : un code L42–L45 qui changerait l'ordre des canons verrait son
  rebase refusé (`actions.canon` immuable) — fermé, mais bloqué.

Donc **tous les hôtes et tous les services qui partagent la base** —
exécuteurs (`agent-runner`) de chaque hôte, `ameesh-approve`, `ameesh notify`,
minuteurs de `canon sync`, hooks `agent-mail` v1 — sont arrêtés, puis mis à
jour **ensemble**, avant d'être relancés. Pas d'hôte « en retard ».

**Pas de retour arrière du code sans restauration de la base.** Les
migrations ne se défont pas : remettre l'ancien code sur une base migrée, c'est
le casser (ci-dessus). La sauvegarde prise **avant** `ameesh migrate` est le
seul retour arrière.

### Procédure

1. **Arrêt partout** (chaque hôte, puis [P] le service système) :

   ```bash
   systemctl --user stop agent-runner ameesh-notify      # sur CHAQUE hôte
   sudo systemctl stop ameesh-approve                    # [P]
   ```

   Vérifier qu'aucun bail ne reste vivant : `ameesh list` (aucun `bail:`), ou
   attendre leur échéance.

2. **Sauvegarde de la base, avant toute migration** :

   ```bash
   pg_dump --format=custom --file ~/backups/ameesh-avant-L46-$(date +%F-%H%M).dump "$AMEESH_DSN"
   ```

   (L'exercice de restauration de l'étape 1 vaut aussi ici.)

3. **Code et migrations** : installer ameesh L46 dans le venv de chaque hôte
   (et `/opt/ameesh/venv` pour ameesh-approve [P]), puis, **une seule fois** :

   ```bash
   ameesh migrate            # applique 0030 … 0036
   ```

4. **[P] Rôles SQL relancés** (idempotents, ils échouent sans rien changer si
   le contrat n'est pas tenu) : ameesh-approve lit désormais `actions.canon` et
   `authenticators.canon` (sans quoi il répond 503), le superviseur lit les
   nouvelles colonnes (`mode`, `stop_reason`, `canon`, `canon_id`,
   `session_bindings.pid_start`…) :

   ```bash
   PGOPTIONS='-c search_path=<schéma>' psql "<DSN admin>" -v ON_ERROR_STOP=1 -f deploy/sql/role-approve.sql
   PGOPTIONS='-c search_path=<schéma>' psql "<DSN admin>" -v ON_ERROR_STOP=1 -f deploy/sql/role-superviseur.sql
   ```

5. **Reclasser à la main les agents externes existants.** 0030 donne
   `mode = execute` à toute ligne existante ; seul un agent NÉ d'un hook sans
   bail naît `externe`. Chaque session humaine déjà au registre (orchestrateur
   interactif, session Codex ou Claude d'un humain) doit être reclassée, sans
   quoi l'exécuteur pourrait la réclamer et reprendre sa session :

   ```bash
   ameesh set <agent> mode=externe       # un agent externe exige un responsable humain
   ```

   (Depuis L46, l'exécuteur ne réclame jamais un agent `externe`, et un hook
   sans bail ne touche plus que `last_seen` sur la ligne d'un agent `execute`.)

6. **Relance partout** (`systemctl --user start agent-runner ameesh-notify` ;
   [P] `sudo systemctl start ameesh-approve`), puis la **vérification**
   ci-dessous.

### Changements visibles

* **`send all`** ne va plus qu'à l'**équipe** (ou au chantier) de
  l'expéditeur (L36) ; un expéditeur sans équipe ni chantier garde la
  diffusion globale.
* **`work add --assignee` / `work assign`** refusent un nom **inconnu** du
  registre et un agent `externe` sans `--externe` (L37, règle 2 de 0030) ;
  L46 : un nom nu d'humain connu est normalisé en `human:<id>`, un canon
  momentanément invalide ne bloque plus, et le refus dit quoi faire
  (`agent-runner register …`, `human:<id>`, `--externe`).
* **`max_agents` physique** (le plus strict des fiches `Host` des canons de
  l'hôte, L43) **plafonne l'exécuteur** : au-delà, les agents restent en
  attente (journal de l'exécuteur), rien n'est arrêté.
* `ameesh mail bind` refuse un agent `execute` sauf `--force` ; `ameesh adopt`
  révoque les liaisons de session de l'agent adopté (L46).
* Hooks `agent-mail` v1 et pont local : voir la section « Passer les hooks à
  la v1 » ci-dessus — le pont est retiré **avant** que `agent-mail` ne passe
  à la v1.

### Vérification après migration

```sql
select mode, status, stop_reason, count(*) from agent_registry group by 1, 2, 3 order by 1, 2, 3;
```

* aucune session humaine en `execute` (sinon : étape 5) ;
* `stop_reason` : NULL pour tout agent qui tourne ; pour les `stopped` /
  `dead` déjà en base, la raison que disait leur texte libre (`manuel`,
  `bail_expire`, `retire_du_canon`), NULL sinon — un ancien arrêt sans raison
  lève `stopped_with_mail` s'il a du courrier : le relancer ou l'arrêter de
  nouveau (`agent-runner stop`, raison `manuel`) ;
* `ameesh canon check` puis `ameesh list` : agents du canon `idle`, canon `ok`
  sur chaque hôte ; `ameesh alerts` : pas de `dead_runner` ni d'`orphan_lot`
  inattendu ;
* ameesh-approve répond (pas de 503) ; `ameesh notify --once --dry-run`.

**Retour arrière** : arrêter de nouveau tous les services, **restaurer la
sauvegarde de l'étape 2** (`pg_restore --clean --if-exists -d "$AMEESH_DSN"
<fichier>`), remettre l'ancien code sur chaque hôte, relancer. Jamais l'ancien
code sur la base migrée.

---

## Accueillir un second canon (Acme) — L45, décision 0031

Pour faire tourner sur `pc-smichea` des agents déclarés dans le canon
Acme (`~/development/acme/home`, fédération
`acme-ingenierie`), à côté de ceux du canon manaty. Les étapes
marquées [P] sont des gestes du propriétaire.

**Prérequis.** ameesh avec les lots L42 à L44 installé ; `ameesh migrate`
appliqué (migrations 0030 à 0035) ; **rôles SQL relancés** :
`deploy/sql/role-approve.sql` et `deploy/sql/role-superviseur.sql` (sans quoi
ameesh-approve répond 503 : il ne peut plus lire `actions.canon`).

1. **[P] Fiches ameesh dans le canon Acme**, par une PR sur
   acme/home, revue et fusionnée :
   - `federation.yaml` : `extensions: {ameesh: {scope: ameesh}}` (seule clé
     libre du schéma OKF Federation) — ameesh ne lit que
     `ameesh/` ; les `type: Agent` de `org/agent-harness/` (sous-agents Claude
     Code) sont ignorés au lieu de rendre le canon invalide ;
   - `ameesh/membres/smichea.md` (Member), `ameesh/hotes/pc-smichea.md`
     (Host : harnais, fournisseurs, modes, `work_dirs` des agents d'Acme ;
     **même `max_agents` que la fiche manaty** — le plus petit des deux
     plafonne tout l'exécuteur) ;
   - `ameesh/agents/<agent>.md` et `ameesh/placements/<agent>-pc-smichea.md`
     pour chaque agent d'Acme (p. ex. `acme-docs`, `acme-securite-docs`).
   Contrôle avant fusion, sur la branche de la PR :
   `ameesh canon check --canon <copie de la branche> --ref HEAD` → 0 erreur.
2. **[P] Configuration de l'hôte** (`~/.config/ameesh/config.json`) : passer
   de `canon`/`canon_ref` à une liste, le canon manaty restant **le premier**
   (canon par défaut : ne pas réordonner dans le même geste) :
   ```json
   "canons": [
     {"path": "/home/smichea/canon", "ref": "origin/main"},
     {"path": "/home/smichea/development/acme/home", "ref": "origin/main"}
   ]
   ```
   La `ref` du second canon est sa branche de confiance pour les
   authentificateurs (L44) ; sans elle, l'amorcer une fois :
   `ameesh canon sync --canon acme-ingenierie --bootstrap-ref main`.
3. **Vérifier**, sans rien lancer :
   - `ameesh canon check` : un bloc par canon, les deux valides ;
   - `ameesh canon sync --fetch` : chaque canon ne touche que ses lignes ;
     aucun agent manaty arrêté ;
   - `ameesh placement check --canon acme-ingenierie` : les agents
     d'Acme admis sur `pc-smichea` ;
   - `ameesh hosts` : origine de chaque limite (`[manaty]`,
     `[acme-ingenierie]`).
4. **Changer un agent de canon** (p. ex. un agent d'Acme déclaré à titre
   provisoire dans le canon manaty, ou `coordinateur`) : [P] retirer sa
   fiche du canon manaty (PR fusionnée, l'agent est arrêté « retiré du
   canon », son nom est libéré), puis [P] la déclarer dans le canon
   Acme : à la synchronisation suivante, la ligne change de canon et
   repart, **avec sa boîte et son historique**. Dans l'autre ordre, le second
   canon reçoit `canon-name-conflict` (local à la fiche) et n'écrit rien.
5. **Agents** : `ameesh resume <agent>` (ou `ameesh adopt` pour une session
   interactive existante) pour les mettre en route sous l'exécuteur.

**Retour arrière.** Retirer le second canon de `canons` (ou revenir à
`canon`/`canon_ref`) : l'exécuteur passe son état à `invalid`
(`close_unconfigured`), ses agents ne sont plus réclamables, **rien n'est
arrêté ni effacé** ; le canon manaty n'est pas touché. Les lignes du second
canon restent dans le registre avec leur colonne `canon`, prêtes pour un
nouvel essai.

## Mise à jour vers L60–L74

Les lots L60, L61, L62, L70, L71, L72 et L74 apportent deux migrations,
**0041** (L60 : `agent_registry.context_max_tokens`, `turn_costs.spend_key`
et son index unique) et **0042** (L70 : `budget_limits`, `budget_events`,
déclencheur `NOTIFY ameesh_budget`). Le paquet dépend désormais de psycopg
(L61). Deux scripts, lancés par le propriétaire depuis le poste, font chaque
geste en étapes séparées, s'arrêtent à la première erreur et n'affichent
aucun secret : [`deploy/mise-a-jour/poste.sh`](../deploy/mise-a-jour/poste.sh)
et [`deploy/mise-a-jour/vm.sh`](../deploy/mise-a-jour/vm.sh). Ils lisent
leurs paramètres (adresse du serveur, personas du serveur, exécuteurs de
l'ancienne base) dans `~/.config/ameesh/bascule.env`, celui de la bascule
L57, hors du dépôt.

**Contrairement à L36–L46, rien ne casse l'ancien code.** 0041 et 0042
n'ajoutent que des colonnes nullables, un index unique sur une colonne que
l'ancien code n'écrit pas (NULL partout), et des tables neuves. Un exécuteur
encore à l'ancienne version tourne donc sans erreur sur une base migrée :
on migre une fois, puis on redémarre chaque exécuteur **à son premier instant
hors tour**, sans arrêt général. Le retour arrière du code ne demande pas de
restaurer la base.

**Ce que verra l'exploitation après le redémarrage.**

* **L60** : une session DeepSeek qui a relu plus de 15 M jetons à son dernier
  tour est tournée avec résumé de reprise **avant le tour suivant**. Les
  sessions longues des DeepSeek tourneront donc au premier tour qui suit la
  mise à jour : c'est attendu. Réglage par agent :
  `ameesh set <agent> context_max_tokens=…` (0 = désactivé).
* **L74** (décision 0034) : le choix du compte au forfait change de règle
  (capacité qui expire le plus tôt, continuité de session). Une bascule de
  compte au premier tour est possible, journalisée avec sa raison.
* **L70** : `ameesh budget` montre les plafonds en vigueur. Base vide : la
  configuration de l'hôte et les défauts s'appliquent comme avant.
* **L72** : un exécuteur survit à une coupure passagère de la base (statut
  « base injoignable », reprise seule).

### Poste — `deploy/mise-a-jour/poste.sh`

Le venv `~/.local/share/ameesh/venv` est installé **non éditable**, depuis
un instantané `~/.local/share/ameesh/src-<sha>` (lien `src-current`).
L'étape `installer` suit le même usage.

1. `poste.sh verifier` : bases visées (sans mot de passe), exécuteurs, accès
   SSH au serveur. Rien n'est modifié.
2. `poste.sh sauvegarder` : `pg_dump -Fc` de la base du mesh (celle du
   serveur) depuis le poste, sauvegarde du serveur (`ameesh-sauvegarde`), et
   `pg_dump` de l'ancienne base, dans `~/backups/ameesh/`.
3. `poste.sh installer [REF]` : instantané du commit REF, installé avec ses
   dépendances (psycopg). La version précédente est notée pour le retour.
   Les exécuteurs en cours gardent l'ancien code en mémoire : enchaîner sans
   attendre.
4. `poste.sh migrer` : `ameesh migrate` **une seule fois** pour la base du
   mesh, partagée par le poste et le serveur. Puis l'ancienne base, avec sa
   propre configuration (`config-ancienne-base.json`) : **oui, elle aussi**.
   Le démarrage de l'exécuteur ne vérifie que la présence du schéma, mais le
   nouveau code écrit `turn_costs.spend_key` à chaque tour et lit
   `context_max_tokens` : sans 0041, ses tours échoueraient.
5. `poste.sh role` : `deploy/sql/role-superviseur.sql` (colonnes de 0041 et
   tables de 0042 au contrat) réappliqué sur chaque base **où le rôle existe
   déjà**. Sur le serveur, en superutilisateur local, par SSH. Un rôle
   absent n'est pas créé : c'est une décision à part.
6. `poste.sh redemarrer` : chaque `ameesh-runner-agent@<p>` actif est
   redémarré **un par un**, dès que `ameesh show <p>` ne dit plus `running`.
   Une persona longue en tour ne bloque pas les autres : le script repasse
   sur la liste (au plus `ATTENTE_MAX`, 2 h). Puis `ameesh-notify`, puis les
   exécuteurs de l'ancienne base (`UNITES_RESTE`), lus avec sa configuration.
   Une unité qui ne reste pas active arrête le script.
7. `poste.sh controler` : `ameesh doctor`, `ameesh budget`,
   `ameesh accounts list`, `ameesh projects`, `ameesh alerts`, unités en
   échec, `doctor` de l'ancienne base.

### Serveur — `deploy/mise-a-jour/vm.sh` (depuis le poste)

Sur le serveur (profil L55), ameesh est un clone détaché dans
`/opt/ameesh/src`, installé non éditable dans `/opt/ameesh/venv`. Les
personas tournent sous `ameesh-runner@<p>` (service système, utilisateur
`ameesh`). **Pas de migration ici** : la base du serveur est celle que
`poste.sh migrer` vient de migrer.

1. `vm.sh verifier` : commit et version installés, unités, migrations
   passées.
2. `vm.sh installer [REF]` : le serveur récupère REF chez l'origine ; s'il ne
   le trouve pas (branche non poussée), il le reçoit en paquet git par SSH.
   Extraction détachée, installation avec psycopg. Le commit d'avant est
   noté dans `/opt/ameesh/precedent`.
3. `vm.sh redemarrer` : refuse tant que 0041 et 0042 ne sont pas passées,
   puis redémarre chaque persona du serveur hors tour, une par une.
4. `vm.sh controler` : unités actives, quinze lignes de journal par persona,
   `ameesh show`, `ameesh doctor` sur le serveur.

Attention : `/opt/ameesh/src` ne garde que les fichiers du commit installé.
Si ce commit ne contient pas encore `deploy/serveur/` (L55, L57), ces
fichiers disparaissent du clone. Les copies installées (gabarit systemd,
script de sauvegarde) restent en place, mais `installer-serveur.sh` n'est
plus rejouable depuis ce clone tant que L55 et L57 ne sont pas dans la
version installée.

### Retour arrière

* **Code, poste** : `poste.sh retour` réinstalle l'instantané précédent
  (`src-<sha>` d'avant), puis redémarre comme à l'étape 6.
* **Code, serveur** : `vm.sh retour` remet le commit de
  `/opt/ameesh/precedent`, puis redémarre.
* **Base** : rien à défaire. L'ancien code ignore les colonnes et les tables
  de 0041 et 0042. Les plafonds posés par `ameesh budget` ne sont plus
  appliqués par l'ancien code : il reprend ceux de la configuration.
  Restaurer une sauvegarde de l'étape 2 (`pg_restore --clean`, exécuteurs
  arrêtés) n'est utile qu'en cas de données abîmées. Ce retour perd tout ce
  qui a été écrit depuis.

## Mise à jour vers 1.6.0

La 1.6.0 réunit L73 (ménage automatique), L94 (sous-utilisation et
`balance_low`), L95 (budget juste, `ameesh cost correct`), L96 (feuille de
route, `ameesh plan`, `ameesh work plan`), L105 (plafonds en cours de tour)
et L106 (coupure de l'hôte). Elle apporte cinq migrations, qui suivent 0042 :

| Migration | Lot | Contenu |
|---|---|---|
| **0043** `grand_livre_corrige` | L95 | `turn_costs.void_reason`, table `turn_cost_corrections` (journal append-only) |
| **0044** `feuille_de_route` | L96 | dates prévues de `work_items` et `work_packages`, table `commitments`, index du fil par expéditeur |
| **0045** `menage` | L73 | colonnes du `/tmp` dans `host_resources`, tables `managed_worktrees` et `housekeeping_log` |
| **0046** `plafonds_du_tour` | L105 | `agent_registry.turn_max_seconds`, `turn_mail_max` |
| **0047** `alimentation_hote` | L106 | `host_resources.on_ac`, `battery_percent` (développée sous le numéro 0106) |

**Comme pour L60–L74, rien ne casse l'ancien code** : des colonnes
nullables ou avec défaut, et des tables neuves. Un exécuteur 1.5.x tourne
sans erreur sur une base migrée. **L'inverse est faux** : un exécuteur 1.6.0
écrit les colonnes de 0045 et 0047 à chaque relevé de l'hôte, et lit celles
de 0046 à chaque tour. On migre donc **avant** de redémarrer, jamais après.

Les scripts sont ceux de L60–L74, dans le même ordre ; seul le contrôle des
migrations de `vm.sh redemarrer` change (0041 à 0047).

1. **Poste** : `poste.sh verifier`, `sauvegarder`, `installer [REF]`.
2. **Migrations** : `poste.sh migrer` applique 0043 à 0047 sur la base du
   mesh, une seule fois, puis sur l'ancienne base. Contrôle :
   `ameesh doctor` ne signale plus de migration manquante.
3. **Rôle superviseur** : `poste.sh role` réapplique
   `deploy/sql/role-superviseur.sql` sur chaque base où le rôle existe. Le
   contrat lit désormais `turn_costs.void_reason` et `turn_cost_corrections`
   (0043), les dates prévues et `commitments` sans sa note (0044),
   `managed_worktrees`, `housekeeping_log` et les colonnes du `/tmp` (0045),
   les plafonds du tour (0046) et l'alimentation de l'hôte (0047).
   `commitments.note` est un contenu : il n'est lu qu'avec
   `role-superviseur-contenus.sql`, appliqué à part, sur décision. Sans
   cette étape, un superviseur échoue sur les nouvelles colonnes, sans rien
   casser d'autre.
4. **Arrêter l'agent fantôme `orchestrator`**, avant le redémarrage. Ce nom
   est inscrit au registre sans exécuteur qui le mène. Après la mise à jour,
   `ameesh doctor` le signalerait comme agent mené sans unité (L106), et les
   alertes de vivacité et de sous-utilisation le compteraient :

   ```bash
   ameesh show orchestrator            # vérifier : aucun tour, aucun lot ouvert
   agent-runner stop orchestrator      # raison d'arrêt `manuel`, jamais relancé seul
   ```

   Un lot encore assigné à `orchestrator` est réassigné d'abord
   (`ameesh work assign <lot> <agent>`).
5. **Drop-in PATH pour l'exécuteur de l'ancienne base.** Les unités
   `ameesh-runner-agent@` et `ameesh-notify` du dépôt portent un PATH
   explicite depuis L106 : au démarrage de l'hôte, elles partent avant que
   la session n'importe le sien (harnais introuvable, `node` absent pour
   `dsh`). L'unité de l'ancienne base (celle de `UNITES_RESTE`) a été écrite
   à la main, sans ce PATH. On lui ajoute un drop-in, sans réécrire l'unité :

   ```bash
   u=<unité de l'ancienne base>        # ex. ameesh-runner-<base>
   mkdir -p ~/.config/systemd/user/$u.service.d
   cat > ~/.config/systemd/user/$u.service.d/path.conf <<'INI'
   [Service]
   Environment=PATH=%h/.local/share/mise/shims:%h/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/bin:/bin
   INI
   systemctl --user daemon-reload
   systemctl --user show $u -p Environment    # le PATH y figure
   ```

   Le redémarrage de l'étape suivante le prend en compte.
6. **Redémarrage des exécuteurs** : `poste.sh redemarrer` (un par un, hors
   tour, puis `ameesh-notify`, puis l'ancienne base), puis, depuis le poste,
   `vm.sh installer [REF]` et `vm.sh redemarrer`, qui refuse tant que 0041 à
   0047 ne sont pas toutes passées.
7. **Contrôle** : `poste.sh controler`, `vm.sh controler`, puis sur chaque
   hôte :

   ```bash
   ameesh doctor --harness     # binaires des harnais résolus, chemin et provenance
   ameesh doctor               # aucun agent mené sans unité d'exécuteur activée
   ameesh hosts                # /tmp et alimentation dans le relevé
   ameesh alerts
   ```

   `doctor --harness` doit trouver chaque harnais **par le PATH de l'unité**
   ou ses emplacements connus, pas par celui d'un terminal ouvert.

**Ce que verra l'exploitation après le redémarrage.**

* **L105** : un tour d'agent mené est clos au point sûr après 30 min
  (`AMEESH_TURN_MAX_SECONDS`), et le hook de courrier remet au plus
  5 messages par tour (`AMEESH_TURN_MAIL_MAX`). Les tours longs habituels
  seront coupés et reprendront au tour suivant, dans la même session. Par
  agent : `ameesh set <agent> turn_max_seconds=… turn_mail_max=…` (0 =
  sans borne).
* **L73** : les tours reçoivent un `TMPDIR` par agent et par session, sous
  `<état>/.menage/tmp`. Les worktrees créés pendant un tour sont retirés à
  la fin de leur lot s'ils sont propres et poussés. `ameesh menage` est en
  essai par défaut ; rien hors des dossiers marqués n'est supprimé.
* **L94** : de nouvelles alertes (`plan_underused`, `idle_capacity`,
  `orchestrator_held`, `host_underused`, `balance_low`), poussées par
  `ameesh notify` par défaut. Une première vague est attendue.
* **L95** : la page d'avancement sépare la dépense payée au token de la
  valeur consommée sur les forfaits. `ameesh cost correct` est un essai en
  lecture seule ; `--apply` corrige le grand livre en une transaction,
  après relecture de l'essai.
* **L96** : le Gantt en tête de `ameesh progress`, les engagements datés
  (`ameesh plan`) et l'alerte `engagement_overdue`.
* **L106** : un hôte qui n'est pas prêt (harnais introuvable, base
  injoignable) met ses agents en « hôte non prêt » sans les arrêter
  (`host_not_ready`). Sur batterie, plus de nouveau tour sous 25 %, arrêt
  propre sous 10 % (`host_power_low`).

**Retour arrière** : comme pour L60–L74 (`poste.sh retour`, `vm.sh
retour`). La base reste migrée : l'ancien code ignore les colonnes et les
tables de 0043 à 0047. Les corrections de `ameesh cost correct --apply`
restent écrites ; l'ancien code ne lit pas `void_reason` et recompte donc les
lignes écartées.

## Mise à jour vers 1.6.2

La 1.6.2 apporte L117 (amendement de 0034) : sans migration, un simple
redémarrage des exécuteurs. **Ce que verra l'exploitation** : la prochaine
nouvelle session d'un agent au forfait ira au compte le plus en retard sur
son rythme, en premier à un compte jamais utilisé. Une bascule
(`account_switches`, type `bascule`) est donc attendue vers le secondaire,
puis le tertiaire, journalisée avec sa raison (« sans relevé : 0 % utilisé,
le plus en retard sur son rythme ; avant : … »). Les sessions en cours
restent sur leur compte tant qu'il est sous son seuil. Vérifier, avant la
mise à jour, que les identifiants des comptes jamais utilisés sont présents
(`ameesh accounts list`, état `ok`) : c'est la première fois qu'ils serviront.
`ameesh --version` (et la première ligne de `ameesh doctor`) dit la version
installée : vérifier « ameesh 1.6.2 » sur chaque hôte après la mise à jour.
Retour arrière : l'ancien code, sans autre geste.

---

## Risques et parades

| Risque | Parade |
|---|---|
| deux boucles sur une même session | arrêt v0 **et mort du groupe** avant tout démarrage v1 (et l'inverse) ; bail exclusif côté v1 ; `ameesh list` après chaque étape |
| la base tombe pendant la bascule | la CLI repasse sur les fichiers v0 avec un avertissement ; les hooks restent silencieux |
| un message importé deux fois | `import-v0` déplace les fichiers dans `imported/` et ne réécrit pas un message déjà au fil |
| canon invalide ou illisible | aucune nouvelle réclamation des agents du canon (fail closed) ; baux et tours en cours intacts ; `ameesh canon check` dit pourquoi |
| un agent mal placé ou sans responsable | non réclamable (R14, C4) ; raison dans `status_text` |
| mise à jour d'un hôte sans dossiers de travail déclarés (L31) | repli transitoire sur l'ancien `cwd` (L35) ; arrêter l'exécuteur ancien, installer L35 et valider avec lui **avant** de publier `work_dirs` / `{agent}` ; `canon check` avertit `admission-cwd-inherited` / `host-work-dir-missing` |
| retour à 1.3.0 avec un canon L35 | chemins `{agent}` littéraux, `work_dirs` ignoré : rendre le canon compatible (un dossier par équipe) et arrêter les agents qui ont besoin d'un worktree propre **avant** tout sync 1.3.0 |
| une session externe prend l'identité d'un autre agent par son dossier (hooks v0) | hooks passés à la v1 (décision 0030, L41) : identité par `AGENT_MAIL_NAME` ou liaison de session explicite, PID ancêtre contrôlé ; sans liaison, rien n'est remis |
| une action irréversible sans humain | la porte exige un reçu lié à l'empreinte, à usage unique, avec échéance ; issue inconnue → `reconcile`, jamais de nouvelle tentative automatique |
| ameesh-approve exposé trop tôt | rien n'est mis en ligne avant la revue de L7 ; exposition = acte du propriétaire ; service sur la boucle locale seulement |
| faux enrôlement de passkey | l'enrôlement n'est qu'une proposition ; confirmation hors bande et PR revue du canon avant toute activation |
| PR non fusionnée qui se déclare canonique (`federation.yaml`) | la branche de confiance vient de l'hôte, du dernier commit appliqué ou de l'amorçage, jamais du commit lu : refus |
| deux `canon sync` concurrents (deux hôtes, un ancien canon) | verrou du registre pris avant le contrôle, monotonie : un ancien commit ne réactive jamais une passkey retirée |
| reçu rejoué | nonce consommé au lancement, en base (`mesh_consumed_nonces`) |
| le disque du PC meurt | `pg_dump` quotidien hors du PC, exercice de restauration de l'étape 1 |
