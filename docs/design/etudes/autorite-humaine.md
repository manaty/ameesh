---
type: Study
title: "Étude — autorité humaine : approuver depuis le téléphone"
description: "WebAuthn/passkeys pour signer une approbation liée à l'empreinte d'un artefact ; ce qui existe ; la brique ameesh-approve à créer."
status: draft
tags: [autorite, webauthn, passkey, securite]
generated: { by: "mesh-design-etude/claude-opus-5-5", at: "2026-10-03T23:30:00+02:00" }
stale_after: 2027-04-03
sources:
  - { resource: "https://www.w3.org/TR/webauthn-4/", title: "WebAuthn Level 4 (FPWD)" }
  - { resource: "https://github.com/w3c/webauthn/issues/1386", title: "Retrait de txAuthSimple" }
  - { resource: "https://www.w3.org/TR/2026/CRD-secure-payment-confirmation-20260702/", title: "Secure Payment Confirmation" }
  - { resource: "https://github.com/duo-labs/py_webauthn", title: "py_webauthn (BSD-3)" }
  - { resource: "https://github.com/Yubico/python-fido2", title: "python-fido2 (BSD-2)" }
  - { resource: "https://github.com/MasterKale/SimpleWebAuthn", title: "SimpleWebAuthn (MIT)" }
  - { resource: "https://privacyidea.readthedocs.io/en/latest/tokens/tokentypes/push.html", title: "privacyIDEA push" }
  - { resource: "https://github.com/binwiederhier/ntfy", title: "ntfy" }
---

# Problème

ameesh v1 signe l'autorité du propriétaire avec une clé Ed25519 dans un
fichier `0600` sur le PC. Les agents tournent sous le même utilisateur Unix :
ils peuvent lire ce fichier. La preuve ne vaut rien tant que la clé est à leur
portée.

# Recommandation

**WebAuthn (passkey) comme mécanisme open source par défaut**, à trois
conditions : la page d'approbation s'ouvre **sur le téléphone**, jamais sur le
PC des agents ; le challenge **est** l'empreinte du reçu canonique ; la
vérification de l'utilisateur (UV) est obligatoire.

| Niveau | Authentificateur | Exigence |
|---|---|---|
| standard | passkey de téléphone (synchronisée iCloud/Google, BE=1) | UV |
| élevé | clé FIDO matérielle (YubiKey en NFC) | attestation vérifiée (FIDO MDS), BE=0 |

Plus tard : une petite application native (Android Keystore/StrongBox, Secure
Enclave) qui affiche elle-même l'objet et signe en ES256 — seul vrai « ce que
vous voyez est ce que vous signez ». C'est le modèle des clés d'appareil de
Nexlink.

# Reçu proposé

```
challenge = SHA-256("ameesh-approval/1\0" || JCS(request))     # JCS = RFC 8785
request   = { v, rp_id, approver, action, decision: approve|deny,
              subject: { type, locator, digest: "sha256:…" },
              params, summary_digest, requested_by, nonce, iat, exp }
receipt   = { v: "ameesh-receipt/1", request, credential_id,
              webauthn: { authenticatorData, clientDataJSON, signature, userHandle } }
```

Vérification : recalculer le challenge ; contrôler `type`, origine autorisée,
`crossOrigin` faux ; `rpIdHash`, UP=1, UV=1 (BE=0 au niveau élevé) ; retrouver
la clé dans un **registre de confiance** que les agents ne peuvent pas modifier ;
vérifier la signature ; appliquer la politique (rôle autorisé) ; échéance ;
consommation atomique du nonce ; recalcul de l'empreinte de l'artefact au
moment d'exécuter. `py_webauthn` le fait hors ligne.

# Ce qui existe

Aucun outil open source trouvé ne produit un reçu signé par l'humain, lié à
l'empreinte d'un artefact et vérifiable hors ligne. privacyIDEA (le téléphone
ne signe que `challenge|serial`, pas le texte), Keycloak CIBA (un jeton, pas un
reçu), Authelia/authentik (second facteur), ntfy (notification sans
signature), Approva et Lelu (approbations d'agents sans liaison
cryptographique). Ce sont des transports ou des briques, pas la solution.

# Brique à créer : ameesh-approve

Service autonome (Python, py_webauthn ou fido2), **sur un hôte ou un
utilisateur Unix distinct des agents** :

1. ameesh soumet `request` ;
2. le service **reconstruit lui-même le résumé** depuis l'artefact (diff git par
   SHA, montant…), jamais depuis le texte de l'agent ;
3. notification au téléphone (ntfy, Matrix, e-mail, Nexlink) avec un lien à
   usage unique ;
4. la page affiche action, résumé, empreinte courte ; le JS recalcule le
   challenge ;
5. `navigator.credentials.get`, `userVerification: "required"` ;
6. reçu renvoyé, vérifié et consommé par ameesh.

# Menaces et parades

| Menace | Parade |
|---|---|
| un agent pilote le navigateur du PC (computer use, CDP) et déclenche l'approbation hybride (QR code) | n'enrôler que des authentificateurs téléphone ou matériels ; refuser les demandes nées sur le PC ; « on ne scanne jamais un QR d'approbation » |
| authentificateur virtuel CDP, faux enrôlement | registre de confiance modifiable seulement par cérémonie hors bande (clé matérielle d'administrateur, ou PR revue du canon) ; attestation au niveau élevé |
| résumé falsifié, serveur compromis | rendu calculé depuis la source, `summary_digest` dans le reçu, service isolé ; risque résiduel levé par l'app native |
| rejeu | nonce, `exp` court, consommation atomique, recalcul de l'empreinte à l'exécution |
| compte iCloud/Google compromis | niveau élevé : BE=0, clé matérielle |

# Non vérifié

Ed25519 sur PIV/OpenPGP selon les firmwares YubiKey ; passkeys liées à
l'appareil sous Android ; licences de pi-authenticator et d'authentik ;
SPC sur iOS ; contenu interne d'Approva et Lelu.
