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
   donne jamais d'identité.

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
EnvironmentFile=%h/.config/ameesh/env        # AMEESH_HOST, AMEESH_CANON, AMEESH_APPROVE_*
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
```

Après chaque PR fusionnée au canon : `ameesh canon sync --fetch` sur chaque
hôte (à la main ou par un minuteur).

**Le service de surveillance** des orchestrateurs, service utilisateur à côté
de l'exécuteur.

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
| une action irréversible sans humain | la porte exige un reçu lié à l'empreinte, à usage unique, avec échéance ; issue inconnue → `reconcile`, jamais de nouvelle tentative automatique |
| ameesh-approve exposé trop tôt | rien n'est mis en ligne avant la revue de L7 ; exposition = acte du propriétaire ; service sur la boucle locale seulement |
| faux enrôlement de passkey | l'enrôlement n'est qu'une proposition ; confirmation hors bande et PR revue du canon avant toute activation |
| PR non fusionnée qui se déclare canonique (`federation.yaml`) | la branche de confiance vient de l'hôte, du dernier commit appliqué ou de l'amorçage, jamais du commit lu : refus |
| deux `canon sync` concurrents (deux hôtes, un ancien canon) | verrou du registre pris avant le contrôle, monotonie : un ancien commit ne réactive jamais une passkey retirée |
| reçu rejoué | nonce consommé au lancement, en base (`mesh_consumed_nonces`) |
| le disque du PC meurt | `pg_dump` quotidien hors du PC, exercice de restauration de l'étape 1 |
