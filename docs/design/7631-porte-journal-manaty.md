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

## 3. « Go » permanent

Un mandat de nuit ou un go de production daté devient un grant (§8.3) signé
par clé fichier sous accord ; `standing.operations` est borné par
`accord_operations`. Commande à ajouter : `ameesh standing list [--active]`
(opérations, plafond, cumul, échéance). Le lien bénéficiaire/cible du grant
relève du lot §3.5 point 2 de la série.

## 4. Signature de décision

`ameesh decide --key` écrit `mesh_approvals.meta.signed_by` = `owner` (session
humaine) ou `avatar` (session d'agent, `--accord AGENT#SEQ` exigé, vérifié
comme au point 1), et `meta.accord`. La vue `ameesh_decision_proofs` expose
ces deux champs ; les 319 lignes antérieures restent `signed_by` inconnu.

## 5. Vue des gestes réservés

`ameesh action journal --since 7d` : actions des quatre opérations avec go,
reçu et issue. Signalés en plus, quand c'est détectable : une décision signée
par la clé de l'owner sans `signed_by`, et un déploiement de production
(journal des déploiements) sans action `prod-deploy` correspondante.
