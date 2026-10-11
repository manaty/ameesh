# Journal de la conception

## 2026-10-11 (courrier des sous-agents)
* **Correctif** (L133, sans migration) : `agent-mail hook` remettait le courrier d'un agent à ses sous-agents (Claude Code les déclenche avec la session de l'agent), qui l'ignoraient, et le marquait livré : la session principale ne le recevait jamais (au moins cinq messages perdus les 10 et 11/10, relevé dans l'amendement de [0036](decisions/0036-auditeur-interne.md)). Une entrée de hook qui porte `agent_id` (sous-agent ou coéquipier de Claude Code, sous-agent de Codex) ne lit, ne remet, ne marque et n'écrit plus rien ; le courrier attend le prochain hook de la session principale. Limite : le harnais DeepSeek ne passe pas `agent_id` à ses hooks ordinaires. Voir [V1-MAILBOX-RUNNER.md](../V1-MAILBOX-RUNNER.md), section 6.

## 2026-10-11 (décisions du propriétaire)
* **L124, L123** : besoin du propriétaire, « recevoir et répondre de manière centralisée à toutes les demandes de décision » — le 10, des orchestrateurs ont attendu 2 h 43 à 4 h 08 des décisions jamais adressées à un humain. `ameesh decide ask` (rattachée au lot, qui passe en `waiting_human` puis revient ; destinataire : responsable de la fiche du lot, sinon du demandeur ; échéance, urgence, signature exigée), `ameesh decisions` (tous projets, la plus ancienne d'abord), `ameesh decide <id>` (identité humaine seulement, ancêtres compris ; réponse renvoyée au demandeur par courrier, au fil et au lot ; signée avec le mécanisme d'`ameesh approve` si exigée), en-tête d'`ameesh projects`, alertes `decision_pending`/`decision_overdue` poussées par `ameesh notify` ; `ameesh chat`, agent de conversation du propriétaire qui ne prend jamais la session d'un autre agent (consigne versionnée). Sans migration (`agent_mailbox`, `kind = 'request'`). Consigne des orchestrateurs : [ORCHESTRATEUR.md](../ORCHESTRATEUR.md). Hors périmètre : toute délégation de décision à un agent.

## 2026-10-11 (courrier regroupé)
* **L125** (lot n° 126 du suivi, accepté par le propriétaire) : le 2026-10-10, sur Nexlink (13 agents, 4 h), 1 270 messages pour 504 tours, 60 % des tours sous 2 min, 87 % des tours de courrier ouverts par un seul message ; copies à l'orchestrateur pour relais, accusés de réception et diffusions à tous ouvraient chacun des tours. Désormais un agent au repos n'est réveillé qu'au bout d'une fenêtre de regroupement (`AMEESH_MAIL_BATCH`, 90 s ; `ameesh set <agent> mail_batch=…`, état local de l'hôte, sans migration), en un seul tour pour tout ce qui est arrivé ; un message `--urgent` (tout expéditeur ; l'interruption reste réservée aux habilités, 0018) ou d'un humain réveille tout de suite ; accusés (`--ack`, ou reconnus par une règle prudente), copies (`--cc`, en `event`) et diffusions à « all » sont passifs, lus au tour suivant, et ne relancent plus un `Stop` ; les non-lus des alertes ne comptent plus le courrier passif ; alerte `turn_churn` (12 tours de moins de 2 min dans l'heure). Consigne : [ORCHESTRATEUR.md](../ORCHESTRATEUR.md) et pied des tours de courrier.

## 2026-10-11 (fin de tour et redémarrage)
* **Correctif** : la fin d'un tour tuait le travail lancé en fond pendant le tour (suite de tests SQL coupée deux fois, un job relancé, le 2026-10-10 sur un poste de onze agents) ; ce qui tourne encore à la fin d'un tour normal garde un délai de grâce (`turn_grace_seconds`, 20 min, par hôte et par agent, migration 0051), l'agent l'apprend au tour suivant avec les codes de sortie (l'exécuteur est sous-moissonneur), la borne du courrier ne coupe plus un tour qui attend son travail et passe de 5 à 20 messages. Un redémarrage d'exécuteur (`systemctl restart`) tuait le tour en cours : l'arrêt draine désormais (plus de nouveau tour, le tour et son travail de fond finissent, au plus 30 min ; second signal = arrêt immédiat ; unités `KillMode=mixed`, `TimeoutStopSec=35min`). Sous pression de l'hôte (charge 43 pour 12 CPU), plus aucun tour ne partait, orchestrateur compris, et la rotation de session était tentée puis annulée toutes les ~7 s : l'orchestrateur (rôle au canon ou `AMEESH_ALERT_ORCHESTRATORS`) passe sous une pression non critique, la rotation attend la fin de la pression, le journal dit une ligne au début et une à la fin de l'épisode (durée, tours retardés). Voir [EXPLOITATION.md](../EXPLOITATION.md).

## 2026-10-11 (issues GitHub des lots)
* **L126**, accord du propriétaire : chaque lot a son issue GitHub, créée et tenue par ameesh (`ameesh work project-github --app <projet>`, tenue par `ameesh notify` sur l'hôte désigné), dans le dépôt que la configuration de l'hôte associe au projet (`github.projects`) ; un dépôt public ne reçoit que le titre et un résumé public contrôlés (secrets, termes exclus, adresses, chemins, noms d'hôte, identifiants refusés) ; fermeture commentée, `issue_ref` posée, « Closes #n » relie la PR au lot. Sans migration. Consigne : [ORCHESTRATEUR.md](../ORCHESTRATEUR.md).

## 2026-10-11 (courrier en souffrance)
* **Correctif** : 42 messages entre agents jamais livrés, sans qu'aucun expéditeur le sache (27 à « orchestrator », un fantôme créé par l'envoi ; 15 à un agent arrêté dont l'ancienne session écrivait encore sous son nom). Désormais `ameesh mail send` refuse un nom absent du registre sans rien créer (noms proches : distance d'édition, préfixe, orchestrateurs de l'équipe pour un nom de rôle), un destinataire arrêté sauf `--queue` (depuis quand, raison, responsable, agent qui a repris) et une identité d'expéditeur arrêtée (avec `whoami`) ; « all » écarte les agents arrêtés ; un envoi ne touche plus le registre. Alerte `mail_undeliverable` (15 min, urgente : responsable humain et orchestrateurs de l'équipe), messages en souffrance dans `ameesh projects`, et `ameesh mail forward <ancien> <nouveau>` pour vider une boîte morte. Sans migration. Voir [EXPLOITATION.md](../EXPLOITATION.md), « Courrier en souffrance ».

## 2026-10-11 (auditeur en effort maximal)
* **Amendement** de [0036](decisions/0036-auditeur-interne.md), demande du propriétaire : « un agent qui régulièrement analysait l'activité d'ameesh et corrigeait les problèmes, exactement comme ce que nous sommes en train de faire ; il est important que cet agent soit en effort max ». Le modèle n'est plus fixé par la décision : le plus capable que l'organisation attribue à l'auditeur, au niveau d'effort le plus élevé de son harnais, choisi dans sa fiche de canon. Passage court chaque heure et à chaque alerte urgente ; analyse profonde chaque nuit et après chaque incident notable, en quatre angles menés par des sous-agents en lecture seule (chronologie, coût de la coordination, causes dans le code, incidents d'exécution). Un lot par cause racine ; il corrige, fusionne et déploie seul les bugs d'ameesh, CI verte (règle du propriétaire du 2026-10-11), et propose le reste ; garde-fous et mesure de son utilité. Consigne [AUDITEUR.md](../AUDITEUR.md) réécrite. Relevé au passage : `agent-mail hook` remet le courrier aux sous-agents (entrée du hook avec `agent_id`), à corriger avant la première analyse profonde.

## 2026-10-11 (suivi des lots fusionnés)
* **Correctif** (sans migration) : constat du 2026-10-10 — deux lots fusionnés en local sur la branche d'intégration, sans PR, sans gel ni branche déclarée, sont restés `intake` neuf heures, signalés « sans activité » par `stale_lot` et `ameesh projects` ; des événements de lots avaient un acteur vide ; le lot en cours affiché était périmé ; un `promoted` posé par erreur ne se corrigeait pas. Désormais : `ameesh work merged <id> --sha S` (tout état ouvert → `merged`, jalon avec le commit) ; `ameesh work move <id> merged --correct "raison"` (humains, orchestrateurs et agents de conception, tracé) ; le relevé des fusions de l'exécuteur ferme aussi un lot sans branche que désigne un commit de fusion (`ameesh-work: <id>`, ou `#<id>` dans le titre si `git config ameesh.lotRef hash`), sur la cible `git config ameesh.target` (introuvable : une erreur, plus de repli sur main) ; l'acteur des commandes `work` est l'identité liée, sinon « inconnu » averti ; le lot en cours est le dernier lot ouvert cité par l'agent dans son courrier (étiquettes résolues pour tout expéditeur), jamais un lot fusionné ou fermé ; `stale_lot` dit l'action attendue. Consigne : [ORCHESTRATEUR.md](../ORCHESTRATEUR.md).

## 2026-10-11 (résumé de reprise sans autorité)
* **Correctif** : le résumé de reprise n'a pas l'autorité du propriétaire. Le 2026-10-10 à 23:02, après une rotation de session, un agent a relu le « Holds : aucune fusion… » de son propre résumé, livré comme message utilisateur, comme une consigne du propriétaire ; il a refusé une fusion demandée par l'orchestrateur, qui a suspendu toutes les fusions du projet jusqu'à une réponse du propriétaire, absent. Désormais la demande de résumé exige la source de chaque contrainte (qui, quand, quel message, lot ou décision ; une contrainte de lot n'est jamais présentée comme une règle générale), et la session neuve s'ouvre sur un cadre de l'exécuteur suivi du résumé délimité (`<resume-de-session … autorite="aucune">`, balises du résumé neutralisées) : c'est la note de l'agent, sans autorité ; une consigne ou un « hold » qu'il mentionne ne s'applique que si sa source est retrouvée ; en cas de doute, l'agent demande à l'orchestrateur ou à l'auteur et ne suspend jamais seul le travail des autres. Même texte pour les trois harnais (aucun canal système déclaré) ; le brief déterministe d'une bascule de compte (L39) est attribué à ameesh. Voir [EXPLOITATION.md](../EXPLOITATION.md), section « Résumé de reprise ».

## 2026-10-10 (livraison)
* **Consigne** : livraison et déploiements ([ORCHESTRATEUR.md](../ORCHESTRATEUR.md)), décidée par le propriétaire après que 1.6.2 est restée 4 h fusionnée sans déploiement — PR verte qui débloque le propriétaire fusionnée avant tout nouveau chantier ; correction de bug fusionnée et déployée sans attendre le propriétaire (règle du 2026-10-11), autre déploiement préparé entièrement (étiquette, `verifier` poste et VM, `retour` connu) et proposé en premier point ; `poste.sh redemarrer` détaché quand l'agent a un exécuteur sur le poste ; numéros de migration réservés dès le début d'un lot ; essai à blanc du workflow de release sur le SHA exact avant toute étiquette qui publie, jamais de déplacement d'une étiquette publiée.

## 2026-10-10 (affectations suivies)
* **L118** : l'orchestrateur confiait le travail par courrier sans lot (agents « sans lot » dans `ameesh projects`, `idle_capacity` trompée, frise vide) et ses lots restaient ouverts après la fusion directe de leur branche sur la cible, sans PR ni gel. Désormais : `mail send --lot <id|réf>` rattache et assigne le lot au destinataire (garde L37 ; jamais repris à un autre agent), `--new-lot "titre"` le crée, un orchestrateur qui écrit à un agent sans lot est averti ; un lot porte sa branche et sa cible (migration 0050), l'exécuteur constate la fusion par le contenu (commit de fusion, avance rapide, squash, commit de fusion qui cite la branche) et ferme le lot ; `idle_capacity` part aussi en courrier `event` aux orchestrateurs. Consigne : [ORCHESTRATEUR.md](../ORCHESTRATEUR.md).

## 2026-10-10 (jamais à l'arrêt)
* **Décision** (proposée) : [ameesh ne s'arrête jamais, il s'améliore](decisions/0037-jamais-a-l-arret.md). Quand rien n'attend, les agents au forfait dorment et leur capacité est perdue ; une file d'amélioration continue leur donne du travail à valeur attendue, sans humain pour le confier.
* **L119** : `ameesh work backlog add|list` (lots `improvement`, migration 0049 : valeur attendue, score, priorité, équipe, capacités) ; prise automatique à chaque passage d'`ameesh notify`, seulement si aucun lot n'attend l'agent ni le projet, jamais de geste irréversible ou de production ; forfaits d'abord, plafonds, pression de l'hôte, débit par heure compté en base ; alerte `backlog_empty`.

## 2026-10-10 (répartition des comptes)
* **Amendement** de [0034](decisions/0034-consommer-d-abord-ce-qui-expire.md), accord du propriétaire : `ameesh accounts list` montrait le compte Claude primaire à 35 % de sa semaine (rythme 55 %) et les deux autres jamais utilisés, leurs forfaits hebdomadaires perdus. Le primaire, toujours en fenêtre ouverte, gagnait toujours le choix par échéance. Désormais, une nouvelle session va au compte le plus en retard sur son rythme (plus petit `utilisé / rythme` sur sa fenêtre la plus contraignante ; sans relevé : 0 %, en premier) ; l'échéance et l'ordre déclaré départagent. Forçage, continuité, pause et Codex inchangés.
* **L117** (1.6.2) : la règle, sa raison dans le journal de l'exécuteur et dans `ameesh accounts list` (« le plus en retard sur son rythme ; avant : … »).
* **1.6.2** : `ameesh --version` / `ameesh version` (version du paquet, `importlib.metadata`, repli sur `pyproject.toml` dans un arbre source) ; `ameesh doctor` l'affiche en première ligne.

## 2026-10-10 (auditeur interne)
* **Décision** : [auditeur interne](decisions/0036-auditeur-interne.md). Demande du propriétaire : « qu'ameesh ait un auditeur interne qui régulièrement, par exemple une fois par heure, regarde que tout se passe bien, que l'utilisation des ressources est optimale, et adapte les règles si besoin ». Persona `auditeur` (DeepSeek `deepseek-flash`), marge d'action en deux niveaux, consigne [AUDITEUR.md](../AUDITEUR.md). Fiche et placement proposés au canon manaty, mise en service après leur fusion. À ouvrir : un lot pour router les alertes urgentes vers un agent dans `ameesh notify`.

## 2026-10-10 (sous-utilisation)
* **L94** : la sous-utilisation alerte autant que la surcharge (« la surconso comme la sous-conso devrait alerter ameesh ») — `plan_underused` (forfait perdu à la remise à zéro, pertes au sens de L74), `idle_capacity` (agents réveillables au repos pendant que du travail attend, ou que le token travaille), `orchestrator_held` (orchestrateur tenu par `attach` avec du courrier, prolonge 0030), `host_underused` (suggestion de déplacement, 0028) ; poussées par `ameesh notify`. Ajout validé par le propriétaire : `balance_low`, autonomie d'un fournisseur payé au token au rythme réel des relevés de solde (48 h, 20 USD ; urgente sous 12 h ou 5 USD).

## 2026-10-10 (comptes au forfait)
* **Décision** : [consommer d'abord ce qui expire](decisions/0034-consommer-d-abord-ce-qui-expire.md), qui amende 0027 ; lot L74.
* **L74** : comptes au forfait en réservoir — choix par échéance de la capacité inutilisée sous le seuil de rythme, continuité de session, relevé échu à 0 % (même règle que L71), choix journalisés avec leur raison, capacité perdue à la remise à zéro dans `ameesh accounts list`.

## 2026-10-10
* **L60** : rotation de session DeepSeek et plafond de contexte qui agit. Les tours DeepSeek étaient inscrits sans session ni modèle, et leur usage par étape ignoré : jamais de rotation sur la taille, 2,34 milliards de jetons relus en 24 h. Au-delà de 15 M jetons relus au dernier tour (`context_max_tokens`, migration 0041), la session est tournée avec résumé de reprise. Le modèle du tour DeepSeek est connu (défaut du descripteur passé au harnais) : fin du barème `pro` appliqué par prudence, qui gonflait l'estimation d'un facteur 12,7 face au solde. Ligne du grand livre clé par son marqueur comptable : plus de doublon quand un exécuteur s'arrête entre l'écriture et l'effacement.
* **L62** : vue par projet, `ameesh projects` (schéma `ameesh-projects/1`) — agents, état et raison, lot en cours, non-lus, dépense 24 h, forfait ou token, lots ouverts sans agent, projets sans agent actif ; colonne PROJET dans `ameesh list` ; la même vue en tête de `ameesh progress`. Retour du propriétaire : on ne voyait pas quels projets étaient en cours ni qui travaillait sur quoi.

## 2026-10-09
* **Bascule** : deepseek1 à 7 et l'orchestrateur Nexlink menés par l'exécuteur, un service par agent (`ameesh-runner-agent@<nom>`) ; plus aucune boucle v0.
* **L48** (1.4.1) : échecs rapides de tour, attente doublée puis arrêt de l'agent.
* **L49** (1.4.2) : le plafond horaire payé au token ne met plus en pause un agent au forfait (0019 §2) ; constaté quand la dépense DeepSeek a arrêté l'orchestrateur.

## 2026-10-08
* **Fusion et déploiement** : conception (#7), lots L36–L46 (#8) et L47 (#9, périmètre sous `extensions.ameesh`, seule clé libre du schéma OKF Federation) ; migrations 0030–0036 appliquées ; deux canons en exploitation sur un même hôte.
* **Décision** : [plusieurs canons](decisions/0031-plusieurs-canons.md) passe `stable`, modalités confirmées telles quelles.
* **Convention** : un agent ameesh porte un nom de rôle ; les noms numérotés hérités de la v0 (`deepseekN`…) ne sont plus attribués, pour éviter les homonymes entre la boîte v0 et le registre.
* **Version** 1.4.0.

## 2026-10-07 (soir : lots codés, non fusionnés)
* **Relecture indépendante** de l'intégration : 5 défauts importants et une fuite possible de secret, corrigés par le lot L46 (migration 0036) ; `integ/0030-0031` @ 3eb40f9, suite complète verte (1408 tests, psql et psycopg).
* **Lots** L36–L45 codés et intégrés sur la branche locale `integ/0030-0031` (20 commits sur `origin/main`, migrations 0030–0035) ; suite complète verte sur les deux pilotes (1388 tests, psql et psycopg). Rien n'est poussé ni fusionné : relecture et accord du propriétaire attendus.
* **Second canon** : fiches ameesh de la seconde organisation préparées hors dépôt et validées par le code intégré.

## 2026-10-07 (plusieurs canons)
* **Décision** (modalités à confirmer) : [plusieurs canons](decisions/0031-plusieurs-canons.md) et [étude](etudes/plusieurs-canons.md) ; lots L42–L45 ; migration 0032 réservée.

## 2026-10-07 (orchestration et vivacité)
* **Décision** : [pas de travail sans réveil possible](decisions/0030-pas-de-travail-sans-reveil-possible.md) ; lots L36–L41 adoptés.
* **Étude** : [orchestration et vivacité](etudes/orchestration-et-vivacite.md) — blocage d'un chantier par un orchestrateur externe arrêté ; décision proposée et lots L36–L41 ; migrations 0030–0031 réservées.

## 2026-10-05 (persona et harnais)
* **Étude** : [persona et harnais](etudes/persona-et-harnais.md) ; spécification de la fiche Persona, des sessions, de la désignation au harnais et d'une mémoire neutre ; points à trancher dans les [questions ouvertes](questions-ouvertes.md).

## 2026-10-05 (bascule)
* **Décision** : [persona et session](decisions/0029-persona-et-session.md) ; L31 étendu à la refonte du placement (admissions sans position).
* **Décision** : [ressources des hôtes et répartition](decisions/0028-ressources-des-hotes-et-repartition.md) ; lot L31 ; migration 0029 réservée.
* **Décision** : [bascule automatique entre comptes](decisions/0027-bascule-automatique-entre-comptes.md) ; lot L30 ; migration 0028 réservée.
* **Lot** : L29, plan de travail (epics, fermeture sur fusion, projection GitHub) ; migrations réservées 0026 (L29) et 0027 (L26).
* **Décision** : [hébergement d'ameesh-approve par équipe](decisions/0026-hebergement-d-ameesh-approve-par-equipe.md) ; [contrat](ameesh-approve-hebergement-equipes.md) (codex3) ; lot L27.
* **Jalon** : ameesh v1.0.0 publiée (manaty/ameesh, tag v1.0.0) ; bascule étapes 0 à 2 faites (base durable, sauvegarde quotidienne hors du poste, canon manaty/home fusionné), étape 3.1 (utilisateur ameesh-approve) faite.
* **Précision** : [page d'approbation](decisions/0017-approbation-via-page-nexlink.md) servie par Nexlink sur `ameesh.nexlink.ph`.
* **Décision** : [une session neuve par lot](decisions/0025-une-session-par-lot.md) ; lot L26 (parité d'exploitation v0 → v1).
* **Lot** : L25, profil cluster (image, manifestes Kubernetes, rôle superviseur en lecture seule).

## 2026-10-04 (v1 terminée)
* **Jalon** : v1 terminée sur `main` 2ff67ff — tous les lots L1–L9 et L9b fusionnés après revue ; `scripts/test.sh` complet : 652 tests verts sur chacun des deux pilotes (psql : 5 sautés, psycopg : 2 sautés) ; essai de bout en bout vert. Hors v1 : L11, L12, L13 fusionnés.

## 2026-10-04 (v1)
* **Vérification** : `scripts/test.sh` complet sur `main` 9ffb2e2 — 618 tests verts sur chacun des deux pilotes (psql : 5 sautés, psycopg : 2 sautés), base temporaire.
* **Jalon** : v1 fonctionnellement complète sur `main` (d2611b0) — lots L2–L9 et L9b fusionnés après revue ; essai de bout en bout vert ; L1 proposé en v1.1.

## 2026-10-04 (critère d'adoption)
* **Décision** : [critère d'adoption des réglages](decisions/0023-critere-d-adoption-des-reglages.md).

## 2026-10-04 (auto-réparation)
* **Décision** : [auto-réparation](decisions/0022-auto-reparation.md) et [étude](etudes/boucle-auto-reparation.md) ; R23 ; lots L18–L23.

## 2026-10-04 (harnais)
* **Décision** : [catalogue des harnais](decisions/0021-catalogue-des-harnais.md) ; R22 ; lots L16–L17.

## 2026-10-04 (modèles)
* **Décision** : [catalogue et évaluation des modèles](decisions/0020-catalogue-et-evaluation-des-modeles.md) ; R21 ; lots L14–L15.

## 2026-10-04 (vitesse et budgets)
* **Décision** : [vitesse](decisions/0018-vitesse-des-agents.md) et [budgets](decisions/0019-budgets-et-routage.md) (transmises par l'orchestrateur Nexlink) ; exigences R19–R20 ; lots L10–L13.

## 2026-10-04 (revue codex3 de la spec, exposition)
* **Correction** : [spécification](specification.md) — canon lu à la révision fusionnée de la branche canonique ; grant permanent à nonce unique puis réservations atomiques ; invariant de bail à marge d'ordonnancement près ; garantie de la porte limitée en v1.
* **Décision** : [ameesh-approve via une page Nexlink](decisions/0017-approbation-via-page-nexlink.md).

## 2026-10-04 (spec v1)
* **Création** : [spécification d'ameesh v1](specification.md) et plan de lots L1–L9.

## 2026-10-04 (Q1, Q8)
* **Décision** : [stockage](decisions/0016-stockage.md) ; Q1 close. Q8 close (instantané public, voir [0015](decisions/0015-interfaces-des-facades.md)). Plus aucune question n'attend le propriétaire.

## 2026-10-04 (Q6)
* **Décision** : [aucun client nommé, interfaces publiques des façades](decisions/0015-interfaces-des-facades.md) ; Q6 close ; mentions de clients retirées des documents ; Q8 ouverte (historique avant passage public).

## 2026-10-04 (Q5)
* **Décision** : [orchestrateurs à tours et placement gouverné](decisions/0014-orchestrateurs-a-tours-et-placement.md) ; exigence R18 ; Q5 close.

## 2026-10-04 (Q4)
* **Décision** : [licence AGPL-3.0-only avec accord de contribution](decisions/0013-licence-agpl.md) ; Q4 close.

## 2026-10-04 (Q3)
* **Décision** : [abandon de la cérémonie Ed25519, autorité par ameesh-approve](decisions/0012-autorite-par-ameesh-approve.md) ; Q3 close, Q6 ouverte (application native).

## 2026-10-04 (revue codex3, B3)
* **Correction** : R5 — identité d'action stable (clé d'idempotence) distincte du nonce d'autorisation ; nouvelle action après issue inconnue seulement par décision humaine assumant le doublon ; `Message-ID` ramené à une poignée de réconciliation.

## 2026-10-04 (Q2)
* **Décision** : [hébergement configurable et provisionnement par le harnais de l'utilisateur](decisions/0011-hebergement-configurable.md) ; Q2 close, Q1 reformulée.

## 2026-10-04 (revue codex3)
* **Correction** : R8 — pas de délégation d'`approve` aux agents ; approbation permanente bornée signée par un humain.
* **Correction** : R14 et [0008](decisions/0008-agents-dans-le-canon.md) — un agent éphémère hérite de l'humain responsable de son créateur.
* **Ajout** : protocole d'exécution d'une action irréversible (idempotence, issue inconnue, réconciliation) dans les [exigences](exigences.md).

## 2026-10-04
* **Création** : bundle de conception — [exigences](exigences.md), [questions ouvertes](questions-ouvertes.md), dix [décisions](decisions/) du propriétaire prises le 2026-10-03 et cinq [études](etudes/).
