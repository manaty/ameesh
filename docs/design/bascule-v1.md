---
type: Runbook
title: "Bascule vers ameesh v1 (brouillon)"
description: "Plan pas à pas, réversible, pour passer le chantier actuel des scripts v0 à ameesh v1 : canon d'amorçage, approbation par passkey, orchestrateurs à tours."
status: draft
tags: [bascule, runbook, v1]
generated: { by: "mesh-design/claude-opus-5-5", at: "2026-10-04T02:00:00+02:00" }
sources:
  - { resource: "../BASCULE.md", title: "Plan de bascule du banc (remplacé par celui-ci à la fusion de L9)" }
  - { resource: "specification.md", title: "Spécification v1" }
---

# Principes

Ceux de `BASCULE.md` restent valables : un outil à la fois, chaque étape
réversible, la v0 intacte jusqu'à la fin, `ameesh export-v0` pour revenir en
arrière. Changements par rapport au banc : **pas de cérémonie de clé Ed25519**
([0012](decisions/0012-autorite-par-ameesh-approve.md)) ; un **canon
d'amorçage** décrit membres, hôte, agents et placements ; les orchestrateurs
deviennent des agents à tours ([0014](decisions/0014-orchestrateurs-a-tours-et-placement.md)).

Actes réservés au propriétaire (marqués **[P]**) : création de dépôts et
d'utilisateurs système, enrôlement de sa passkey, toute dépense, toute
exposition réseau.

# Étape 0 — sauvegardes (inchangée)

Voir `BASCULE.md` étape 0.

# Étape 1 — base et paquet

Postgres local (profil « local », [0016](decisions/0016-stockage.md)) :
conteneur avec redémarrage automatique et `pg_dump` quotidien hors du PC, ou
paquet système. Venv, `pip install -e`, liens `ameesh`, `agent-runner` ;
`ameesh migrate`, `ameesh doctor --notify-test`. `~/.local/bin/agent-mail`
reste la v0.

# Étape 2 — canon d'amorçage **[P] pour la création du dépôt**

Dépôt de canon du projet (`manaty/home` selon l'accord de principe de
[0005](decisions/0005-canon-okf.md), ou un dossier du dépôt du projet) avec :

- `federation.yaml` (rôles : responsable du projet = `human:smichea`) ;
- un `Member` par humain ;
- un `Host` `pc-smichea` (responsable `human:smichea`, politique : harnais
  claude, codex, deepseek ; modes `subscription` pour claude/codex,
  `api-key` pour deepseek) ;
- un `Agent` et un `Placement` par agent actuel. La liste vient de l'état v0
  (`~/.local/state/nexlink-agents/<nom>/tool`) : claude1–2, codex1–3,
  deepseek1–7, plus les deux orchestrateurs. Un outil `ameesh canon init
  --from-v0` (à ajouter à L2 ou L9) génère ces fiches en brouillon ; le
  propriétaire relit la PR.

Contrôle : `ameesh canon check` sans erreur, puis `ameesh canon sync`.

**Retour arrière** : retirer `AMEESH_CANON` ; le registre garde ses colonnes.

# Étape 3 — ameesh-approve **[P]**

1. Utilisateur système dédié (`ameesh-approve`), distinct de celui des agents.
2. **Exposition au téléphone** : WebAuthn exige un contexte sécurisé (HTTPS ou
   `localhost`) et un domaine stable (RP ID). Le téléphone n'atteint pas le
   `localhost` du PC : il faut une adresse HTTPS joignable par le téléphone —
   par exemple un réseau privé avec certificat (type Tailscale), une page
   Nexlink, ou un petit domaine. **Décision : une page Nexlink**
   ([0017](decisions/0017-approbation-via-page-nexlink.md)) ; la mise en ligne reste un acte du propriétaire.
3. Enrôlement de la passkey du propriétaire : le service produit une PR au
   canon (`Member.authenticators`) ; le propriétaire la fusionne.
4. Essai : une action `shell-noop` de classe `irreversible` reste bloquée sans
   reçu, passe avec un reçu signé sur le téléphone, et le rejeu du reçu échoue.

**Retour arrière** : arrêter le service ; les actions irréversibles restent
bloquées (comportement sûr).

# Étape 4 — un agent pilote

**Jamais deux boucles sur la même session** : la boucle v0 ne connaît pas le bail
v1, et deux harnais reprendraient la même session. Le fonctionnement en double
de l'ancien plan est réservé aux agents factices indépendants (essais).

Pour le pilote réel (`deepseek7`), dans cet ordre :

1. attendre la fin de son tour en cours ; `nexlink-agent stop deepseek7` ;
2. **attendre la mort de tout le groupe de processus** de la boucle v0 (aucun
   harnais restant sur sa session) ;
3. `ameesh import-v0 --agents deepseek7` (courrier, session) sur cet état stable ;
4. `agent-runner --agents deepseek7` ; aller-retour ; vérifier que le message
   apparaît dans le **fil** (`ameesh fil show`).

**Retour arrière, symétrique** : arrêter l'exécuteur v1 pour cet agent
(SIGTERM, bail rendu) et attendre la mort de son groupe ; `ameesh export-v0
--agents deepseek7` ; relancer la boucle v0 sur la même session.

# Étape 5 — les autres agents, par vagues

Même procédure que l'étape 4, agent par agent (arrêt v0 et mort du groupe avant tout démarrage v1). Après chaque vague : `ameesh list`,
`ameesh canon check`, `ameesh fil list`.

# Étape 6 — les orchestrateurs

1. Service de surveillance qui dépose ses événements dans la boîte de
   l'orchestrateur (`kind = event`), à la place du `Monitor` en session.
2. Fin de la session interactive ; l'exécuteur réveille l'orchestrateur par des
   tours (événements regroupés).
3. `ameesh attach orchestrateur` pour parler en direct ; à la sortie, le bail
   revient à l'exécuteur.

**Retour arrière** : `ameesh attach` puis rester en interactif ; relancer la
surveillance en session.

# Étape 7 — supervision (systemd utilisateur)

Comme `BASCULE.md` étape 7, plus le service de surveillance et, sous son propre
utilisateur, `ameesh-approve`.

# Étape 8 — fin de vie de la v0

Comme `BASCULE.md` étape 8.
