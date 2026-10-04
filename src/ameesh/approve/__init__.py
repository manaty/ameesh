# SPDX-License-Identifier: AGPL-3.0-only
"""ameesh-approve — service d'approbation humaine (spec §9, C8, décision 0012).

Un humain approuve UNE action précise depuis son téléphone : il ouvre un lien
à usage unique, voit ce qu'il approuve (résumé calculé par le service depuis
l'action, jamais depuis le texte d'un agent), signe avec sa passkey (WebAuthn,
vérification de l'utilisateur obligatoire). Le service produit un reçu
`ameesh-receipt/1` qu'ameesh vérifie et consomme lui-même (receipts.py).

Le service tourne sous un autre utilisateur Unix que les agents, n'écoute que
sur la boucle locale, et est exposé en HTTPS par un mandataire (page Nexlink,
décision 0017). Modules :

* `config`  — configuration, jeton de service (0600), dossiers privés ;
* `store`   — état en fichiers 0600 (demandes, liens hachés, reçus, enrôlements) ;
* `sources` — lecture de l'action (table `actions` du lot L5, ou en mémoire) ;
* `duplicate` — empreinte de la décision « assumer le doublon » (réplique de
  `actions.assume_duplicate_digest`, lot L5) ;
* `render`  — résumé signé et pages HTML (échappement, CSP stricte) ;
* `enroll`  — attestation WebAuthn → proposition d'entrée de canon ;
* `service` — logique (demande, page, reçu, enrôlement) ;
* `server`  — HTTP (http.server), en-têtes de sécurité, Host, débit, tailles ;
* `cli`     — `ameesh-approve serve | enroll-link | gen-token`.
"""
