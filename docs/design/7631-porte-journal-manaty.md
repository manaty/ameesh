# Porte d'actions en mode journal (Manaty) — #7631

> Lot #7631 (manaty/ameesh#5821), série « avatars, charte et porte d'actions »
> (référence #7627, spec owner du 17/11/2026, §3.4 et §3.5 point 7).
> Décisions owner du 17/11 : quatre gestes passent par la porte (production,
> dépense, publication store ou publique, signature de décision) ; l'owner
> garde sa clé fichier, son accord est son « go » dans la session de son
> avatar (le bureau), qui signe avec la clé ; la porte journalise, elle ne
> bloque pas.

## 1. Reçu par clé fichier sous accord (livré)

- Le canon nomme, sur l'authentificateur `ed25519` d'un humain, les opérations
  admises : `accord_operations: [prod-deploy, …]` dans `Member.authenticators`.
  Colonne `authenticators.accord_operations` (migration 0586, additive).
  Ailleurs que sur une clé `ed25519`, le champ gèle le membre (erreur de canon).
- La demande signée porte `accord = {agent, seq}` : l'entrée du journal de
  conversation (L78) où l'humain a dit « go ». Le champ est dans le challenge.
- Vérification (`receipts._verify`) : façade `ed25519` hors politique admise
  seulement avec `accord`, pour l'opération recalculée par la porte
  (`expected_operation`, toutes les opérations d'un grant) ; l'entrée doit
  être un `human_message` de confiance de l'approbateur, écrite au plus 120 s
  après `iat` et au plus `max_ttl` (24 h) avant. Un « go » plus ancien est une
  approbation permanente (point 3).
  Le message suit une **grammaire fermée** : il commence par « go » et ne
  porte ensuite QUE les références du geste signé — identifiant d'action,
  cible, opération, lot pour une action ; connecteur, toutes ses opérations
  et un mot de mandat (« mandat », « grant », « permanent »…) pour un grant ;
  identifiant de la décision, option et libellé pour `decide` — séparées par
  des espaces ou une ponctuation simple. Tout autre mot refuse l'accord
  (`accord_ref`) : une question, un report (« demain », « attends », « later »)
  ou une négation (« pas », « don't ») ne peut pas se cacher dans la phrase.
  L'opération seule et une autre cible sont refusées. Pour `decide`, le go
  doit désigner **une seule** option, et ce doit être celle qui est signée :
  la clé canonique, ou un libellé entier comparé mot à mot ; un libellé
  contenu dans un autre, une clé égale au libellé d'une autre option ou deux
  libellés identiques laissent deux lectures et sont refusés (à la signature,
  et dès la demande pour les libellés dupliqués). Un texte libre ne nomme
  aucune option : l'avatar ne le signe pas, c'est l'owner qui le signe.
  — relectures cx2, dev3 et auditeur du 18/11 (B1 et B2).
- Un accord (`agent`, `seq`) ne cite **qu'un reçu** : la table
  `accord_claims` (même migration 0586, additive) réserve l'accord au geste
  (`kind`, `ref`) ; la clé primaire tranche la course entre deux reçus et la
  réutilisation par un autre geste est refusée (`accord_ref`). Un grant ne
  s'inscrit donc que sur un go qui le nomme comme mandat.
- Sans `accord_operations` (TPK), rien ne change : refus `facade_refused` ou
  `accord_operation`.
- Outils : `ameesh action request <id> --local --approver human:X --accord
  bureau#123`, puis `ameesh receipt sign <demande> --key <clé 0600>
  --credential-id <id du canon>`, puis `ameesh action approve`.

Limite : l'avatar détient la clé ; la vérification prouve la cohérence du
journal (un go existe, de l'humain, avant la signature), pas que l'humain a
vu le reçu. C'est le contrat du mode journal (décision 8 du 17/11).

## 2. Les quatre opérations (après #7628)

Un module (développement ou Nexlink) déclare un connecteur `journal` et ses
opérations `prod-deploy`, `spend`, `publish`, `decision-sign`, classe
`irreversible` (ou `costly` pour `spend`). Première tranche : le connecteur
n'agit pas. Le bureau propose l'action, la fait approuver par un reçu sous
accord, fait le geste avec ses outils, puis `execute` inscrit la preuve
d'exécution (`args.proof` ajoutée au résultat : URL de run, SHA, identifiant
de ressource). L'action montre qui l'a proposée (`proposed_by`), le go
(`accord` du reçu lié), le reçu, l'issue.

## 3. « Go » permanent (livré)

Un mandat de nuit, un go de production daté ou un cadre de la charte de
l'avatar devient un grant (§8.3) signé par clé fichier sous accord (forme a) ;
`standing.operations` est borné par `accord_operations`. La demande peut
porter `standing.charter = {frame, version}` (signé) : le cadre de charte
inscrit. `ameesh standing register <reçu>` l'enregistre ; `ameesh standing
list [--all]` donne opérations, plafond, usage, échéance, cadre et accord.
Le lien bénéficiaire/cible du grant relève du lot §3.5 point 2 de la série.

Le grant passe aussi le plafond du canon de #7629
(`extensions.ameesh.standing_approvals`) : connecteur et opérations,
approbateurs, durée, montant/devise, bénéficiaire, cibles, et — quand la règle
l'exige — `charter_line`/`charter_version`. Le cadre `{frame, version}` de
#7631 est une référence signée supplémentaire : il ne remplace pas la ligne
et la version de charte exigées par la politique. `ameesh action standing
register <reçu>` suit le même chemin (avec le plafond du canon) ; les deux
commandes enregistrent le même grant.

Accord d'une décision (point 4, validé par le bureau le 17/11) : forme (a)
`{agent, seq}` ; la forme (b) `{grant: id}` est reportée (voir §4).

## 4. Signature de décision (livré)

- `ameesh decide <id> <réponse> --key <clé owner> --accord AGENT#SEQ|grant:ID` :
  seule la session d'un agent déclaré avatar de l'humain (section `avatar` de
  sa fiche, `responsible` résolu) passe la garde « seul un humain répond ».
  Sans `--accord`, la garde est inchangée.
- Accord vérifié avant de signer : message de l'humain au journal, au plus
  24 h avant, go de grammaire fermée qui nomme l'identifiant de la décision
  ET l'option signée (comme au point 1) — un go qui nomme une autre option,
  ou plusieurs, ne couvre pas la signature. Une réponse libre (aucune option)
  n'est jamais signée par l'avatar : l'owner la signe.
- Forme (b), `grant:ID`, refusée dans cette tranche (relecture cx2, B) : un
  grant ne vaut que par le contrat complet des approbations permanentes
  (connecteur, classe, plafond, réservation atomique liée à la signature,
  bénéficiaire et cible de #7629). Elle viendra quand la signature d'une
  décision passera par la porte comme opération `decision-sign` (point 2).
- Une décision sur la clé owner ou une attestation de provenance relève de
  `decision-sign-key` : drapeau du demandeur (`decide ask --key-provenance`),
  reclassement par un humain ou un avatar (`decide reclassify <id>
  key-provenance`, jamais l'inverse), ou au doute le texte (clé owner,
  provenance). Elle ne sera jamais couverte par un grant.
- En base : `mesh_approvals.meta.signed_by` (`owner` | `avatar`), avec `agent`
  et `accord` pour l'avatar ; la réponse gardée porte `signer`. Pour l'avatar,
  `signer` entre dans l'empreinte signée (le changer casse la preuve) ; les
  réponses d'avant et celles de l'owner gardent leur document signé.
- La vue `ameesh_decision_proofs` n'est pas modifiée : le contrat du rôle
  bureau limité (`deploy/sql/_contrat-bureau.sql`) fige sa définition et en
  exclut la méta d'approbation. Les 319 lignes antérieures restent sans
  `signed_by`.

## 5. Vue des gestes réservés (livré)

`ameesh action journal [--since 7d] [--until ISO] [--json]`
(`reserved_gestures.journal`) : pour la période, les actions des opérations
`prod-deploy`, `spend`, `publish`, `decision-sign(-key)` — proposant,
approbateur, accord (go cité par le reçu, ou grant), reçu, issue — et les
décisions signées avec une clé owner, par l'owner ou par son avatar (agent,
accord).

Signalé : une décision signée dont la preuve ne dit pas qui a tenu la clé
(`decision_signer_unrecorded` : avant #7631, ou hors de `ameesh decide`).
Non détectable ici : un déploiement de production fait hors de la porte —
ameesh ne tient pas de journal des déploiements de production (ceux de
Nexlink passent par GitHub Actions). Le module qui déclarera `prod-deploy`
(point 2, après #7628) apportera sa détection.

## Reste

- Point 2 : connecteur `journal` et déclaration des quatre opérations par un
  module, après #7628 ; détection des déploiements de production hors porte ;
  signature de décision par la porte, et alors accord par grant réservé.
- Le mode `extensions.ameesh.action_gate.mode` (#7627) n'est pas lu par ce
  lot : en `journal`, rien n'est bloqué, ce que fait déjà la porte ici.
