# L232d — plan proposé pour les adaptateurs de connexion OAuth (lot #366)

Auteur : mty-amh-impl-04, 2026-10-16 ~11:45Z. Branche `lot/l232d`, base `main` 20aa67782.
Statut : **proposition soumise à mesh-design avant implémentation** (note d'auteur du 14/10
10:03Z). Aucun code écrit, aucune CI, aucun geste distant, aucun root, aucune dépense.
Source de périmètre : étude `docs/design/etudes/l232-autonomous-host-provisioning.md`
(§ « OAuth: ameesh prepares, a human consents in a browser », § « What stays with the
owner », § « Delivery in four implementation lots ») et corps du lot #366.

## 0. État vérifié (première main, 16/10 ~11:30Z)

- `#364` (L232b) **fusionné** : tête `e6f56580743651aebe60b5d8eae73d07a387be84` ancêtre de
  `origin/main` (20aa67782) — `git merge-base --is-ancestor` = oui.
- `#365` (L232c) **non fusionné** : tranche 1 tête
  `339d5c07d0f8a77b338eead8f4f5bb0eeb5494d4` (PR #720) admise au prochain paquet
  (mesh-design 11:01, composeur impl-01 11:2xZ) mais **absente de main** ; tranche 2
  (PR #1475, `39813384`) en relecture hors Claude (R1 codex3/Codex, R2 qa01/DeepSeek).
- Conséquence : les parties de L232d qui dépendent du coffre/transport L232c restent
  bloquées ; le reste attend la validation de ce plan. Le présent document est le seul
  livrable de ce tour.

## 1. Périmètre retenu

1. Demande de connexion durable par `(hôte, harness, compte)`, une seule en attente.
2. Adaptateurs **épinglés** Claude / Codex / `gh`, exécutés dans le foyer isolé du compte,
   sans copie de cache d'authentification.
3. Défi navigateur **privé et expirant** : seuls URL officielle, code à usage unique,
   portée et échéance sont montrés, après authentification de l'humain ; jamais dans le
   fil public, le courrier, les journaux, git ou un rapport.
4. `decide ask` portant hôte, fournisseur, compte, portée, échéance et **lien vers la vue
   privée** — jamais le code ni un jeton.
5. Expiration, annulation, réessai borné, santé d'authentification ; compte tenu
   `auth_blocked` (L226b) tant que la santé n'est pas prouvée.
6. Pilote de bout en bout sur un hôte de test admis : un déplacement `dsh`, puis
   Claude/Codex, un seul bail, caps et garde vérifiés, retour arrière, preuves sans secret.

## 2. Vue privée : faisabilité et porte de réestimation

### Ce que la pile d'approbation existante fournit déjà

- `src/ameesh/approve/store.py` : jetons de lien 256 bits **hachés au repos**, état `0600`
  en dossier `0700`, usage unique atomique (`os.link`, `save_receipt_once`), échéances
  `link_exp`/`exp`, élagage.
- `src/ameesh/approve/service.py` : API de service (jeton de service), demandes relues
  côté service, reçus signés WebAuthn (`receipts.py`), pages CSP strictes (`render.py`,
  `visible()` contre l'usurpation), origine HTTPS publique pour le téléphone.
- `src/ameesh/approve_client.py` : client mesh (TLS local, jeton, création de demande,
  lecture de reçu).

### L'écart à combler

Le modèle de demande est lié à une **action** L5 : `create_request` exige un `action_id`,
recalcule l'empreinte de l'action, exige un authentificateur WebAuthn actif pour
l'approbateur (`service.py:213-220`) et rend une page « signer ». Un défi de connexion est
un **genre nouveau** :

- pas d'action L5, pas d'empreinte d'action ; le résumé signé porte l'identifiant du défi,
  l'hôte, le fournisseur, le compte, la portée et l'échéance — **jamais le code, même
  haché** (l'étude interdit jusqu'au condensat d'un secret dans une preuve) ;
- le code vit dans l'état privé du service (`0600`/`0700`), pas en base, pas dans le fil ;
- la vue est **authentifiée** : le lien seul ne montre rien ; l'assertion WebAuthn de
  l'humain ouvre le défi et signe « terminé » ou « annuler » (les deux sont des reçus,
  donc le service réutilise `receipts.py` avec un genre de demande distinct) ;
- un marqueur « vue ouverte » distinct du reçu, sinon le premier affichage consommerait le
  lien avant la signature.

### Options examinées

- **A (recommandée)** : étendre `ameesh-approve` d'un genre « défi de connexion » — même
  service, même coffre d'état, même origine. Coût : chemin de création, page, TTL, résumé
  signé sans code, marqueur de vue ; service classé **sécurité** (deux avis).
- B : vue privée dans `ameesh serve`/`ui/` — jeton bearer d'hôte partagé, service lié à
  `127.0.0.1`, pas de lien par humain ni d'usage unique : exclue.
- C : code remis par courrier/fil privé — exclue par l'étude et le corps du lot.

### Porte de réestimation

A reste « la pile existante » (aucun nouveau service), mais touche un service de sécurité
et son déploiement : `AMEESH_APPROVE_URL` est **absent de la configuration de cet hôte**
(`ameesh-agents-1`), à vérifier sur l'hôte du pilote. L'estimation de 3 h du lot ne tient
pas telle quelle ; voir §9.

## 3. Où tournent les adaptateurs

- Principe : la connexion se fait **sur l'hôte qui porte le compte**, dans le foyer isolé
  du profil (`accounts.py` : `home`, `config_dir`, `launch_env`), jamais par copie du poste.
- **Recommandation** : exécution **locale par l'exécuteur de l'hôte cible** (le runner y
  tourne déjà sous l'utilisateur du compte) ; le mesh déclenche, l'exécuteur supervise et
  remonte le défi par le canal privé. Pas de nouvelle délégation SSH, pas de root.
- Repli si l'hôte n'a pas encore d'exécuteur : canal SSH **borné et dédié connexion**
  (ForceCommand + dispatcher fixe, privilèges abandonnés vers le compte, patron retenu par
  #365 tranche 2 `ameesh-secrets`), avec sa propre décision owner. Ce repli serait un
  **lot/étape distinct**, pas caché dans L232d.
- Versions épinglées : `harness_versions.probe` / `ameesh harness versions` enregistre la
  version installée par hôte ; l'adaptateur **refuse un flux non reconnu** (signatures de
  sortie par version). Ameesh n'installe ni ne met à jour un CLI (règle L120/L106).

## 4. Adaptateurs par fournisseur

| Fournisseur | Commande épinglée | Capturé (privé) | Fin de flux | Santé |
| --- | --- | --- | --- | --- |
| Codex | `codex login --device-auth` dans le foyer isolé | URL officielle + code à usage unique | processus terminé + sonde d'état privée | `sentinel.probe_codex` ; `auth.json` reste sur l'hôte |
| Claude | flux `/login` (`claude auth login`) | URL d'autorisation + code | état `loggedIn` privé | `sentinel.probe_claude` |
| GitHub | `gh auth login --hostname github.com --git-protocol https --web` | code d'appareil + URL | `gh auth status` privé + `gh auth setup-git` **sur l'hôte** | `sentinel.probe_cli('gh', …)` |

- Device-auth Codex désactivé côté compte/workspace : demande administrateur séparée,
  jamais de repli par copie de cache.
- Claude `setup-token` imprime le jeton et exige le type de compte `oauth_token` recommandé
  par `docs/COMPTES.md` §6 (aucun lot ouvert) : à traiter à part, hors pilote par défaut.
- `gh auth setup-git` écrit le **chemin absolu** du binaire `gh` qui l'exécute (incident
  #1063) : toujours sur l'hôte cible, jamais recopié.
- Sortie privée : stdout/stderr vont dans un fichier `0600` du foyer (ou tube mémoire) ;
  seuls les champs autorisés sortent ; pas de `set -x`, pas de flux brut, pas de
  `gh auth token` dans un rapport ; erreurs en catégories structurées.

## 5. Cycle de vie

- Une seule demande en attente par `(hôte, harness, compte)` ; pas de relance tant qu'elle
  court.
- Superviseur : groupe de processus dédié, échéance bornée (proposition : 15 min, à
  valider), arrêt SIGTERM puis SIGKILL du groupe, effacement du défi (fichier + état du
  service) à l'expiration, l'annulation ou l'erreur.
- `decide ask` : hôte, fournisseur, compte, portée, échéance, lien privé ; options
  `terminé` / `annuler` ; `--needs-signature` selon la portée. Le lien seul n'affiche rien
  sans l'assertion de l'humain.
- Échec/annulation : compte `auth_blocked` (L226b), réessai borné (cooldown, une seule
  relance), nouvelle demande seulement sur geste explicite ou après cooldown.
- Journal : transitions et catégories (`expired`, `cancelled`, `provider_refused`,
  `flow_unrecognized`, `mismatch`, `healthy`), jamais de valeur ni de condensat.

## 6. Intégration au provisionnement

- `host_preflight.check` exige déjà `login:<harness>/<compte>` sain et frais (300 s) et
  l'inventaire `tools`/`secrets` de l'hôte : une connexion réussie doit écrire
  `credential_health` pour que placement et handoff (L232a/e) reprennent. Interface
  inchangée, seule l'alimentation est nouvelle.
- Frontière de tour : les flux `/login`/`gh`/`codex` écrivent dans le foyer du compte ;
  pour un compte déjà utilisé par un tour vivant, la connexion se prépare **hors tour**
  (compte « en préparation »), jamais par bascule de valeur sous un tour vivant (règle
  L232c). L'installation d'un jeton `setup-token` relève de `coffre-release.py` (L232c).
- L232d ne remplace pas le sentinel : il s'appuie sur ses sondes pour la santé après
  connexion, et sur `accounts.check` / `host_preflight` avant de rendre l'hôte prêt.

## 7. Fichiers et tests envisagés

- `src/ameesh/login.py` (nom à confirmer) : superviseur, parseurs épinglés, états,
  catégories, une demande par `(hôte, harness, compte)`.
- `src/ameesh/approve/{service,render,store}.py` + `src/ameesh/approve_client.py` : genre
  « défi de connexion » (création, page authentifiée, marqueur de vue, résumé sans code).
- `src/ameesh/accounts.py` : surface `ameesh accounts login <harness>/<compte>` (nom à
  confirmer), résolution du profil, environnement épuré.
- Tests : `tests/test_l232d_oauth.py` (CLI factices : chaque fournisseur, flux non
  reconnu, expiration, annulation, double demande, reprise, canari absent de
  stdout/stderr/mail/journal/git) ; `tests/test_l232d_challenge_view.py` (lien à usage
  unique, expiration, assertion requise, résumé signé sans code, élagage) ; contrat
  `decisions` (question = lien, pas de code).
- Docs : `docs/L232D-OAUTH-ADAPTERS.md`, section opérateur dans `docs/COMPTES.md`,
  `docs/BASCULE.md` si le genre « défi » doit être déployé.

## 8. Pilote de bout en bout (hôte de test admis ; sans root, sans dépense, sans prod)

1. Adaptateurs factices déterministes (échecs injectés à chaque étape).
2. `gh` d'abord (sans dépense), puis `codex login --device-auth`, puis Claude `/login`
   (consentement navigateur de l'owner).
3. Un déplacement `dsh` d'un agent de test vers l'hôte (handoff L232a) : préflight vert
   **avant** activation ; un seul bail, checkout exact, caps, garde saine, stockage privé,
   un tour terminé.
4. Échec de la cible + retour arrière sans perdre de travail poussé.
5. Canari synthétique : absent de stdout, stderr, mail, journaux, git, artefacts retenus.
6. Preuves : id d'opération, hôte, commit, versions, reçus de décision, catégories et
   durées, santé — par références, jamais de valeur ni de condensat.

## 9. Estimation et découpage proposés

- **Tranche 1 — socle** : vue privée étendue, superviseur et 3 adaptateurs locaux,
  intégration `decide`, expiration/annulation/réessai, tests. **~12 h.**
- **Tranche 2 — pilote** : canal hôte si nécessaire, déplacement `dsh`, Claude/Codex,
  retour arrière, preuves. **~12 h**, hors attente de consentement navigateur et hors
  disponibilité d'un hôte admis.
- Total ~24 h contre 3 h à l'étude. Motifs : la vue privée touche un service de sécurité
  (deux avis, délai de relecture non compté) ; le pilote exige un canal d'exécution sur
  l'hôte cible. À valider par mesh-design.
- Ordre proposé : tranche 1 dès validation (ne dépend pas de #365) ; tranche 2 après
  #365 tranche 1 dans main et arbitrage du canal.

## 10. Questions à mesh-design

1. **Vue privée** : validez-vous l'extension d'`ameesh-approve` (genre « défi », vue
   ouverte par assertion WebAuthn, résumé signé sans code) plutôt qu'un nouveau service ?
2. **Canal hôte** : exécution locale par l'exécuteur de l'hôte cible (recommandée) ou
   canal SSH borné dédié ? Dans le second cas, lot distinct ?
3. **Claude au pilote** : `/login` seulement, ou `setup-token` (alors ouvrir le type de
   compte `oauth_token` de COMPTES.md §6) ?
4. **Réestimation** 12 h + 12 h et découpage en deux tranches : accord ?
5. **Hôte du pilote** : lequel, et le déplacement `dsh` est-il dans le périmètre
   d'admission L232a/e existant ?
6. **Frontière #365 tranche 2** : le canal connexion partage-t-il le dispatcher/ForceCommand
   `ameesh-secrets`, ou reste-t-il séparé ?
