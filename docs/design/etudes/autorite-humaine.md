---
type: Study
title: "Study — human authority: approving from the phone"
description: "WebAuthn/passkeys to sign an approval bound to an artefact's digest; what exists; the ameesh-approve building block to create."
status: draft
tags: [autorite, webauthn, passkey, securite]
generated: { by: "mesh-design-etude/claude-opus-5-5", at: "2026-10-03T23:30:00+02:00" }
stale_after: 2027-04-03
sources:
  - { resource: "https://www.w3.org/TR/webauthn-4/", title: "WebAuthn Level 4 (FPWD)" }
  - { resource: "https://github.com/w3c/webauthn/issues/1386", title: "Removal of txAuthSimple" }
  - { resource: "https://www.w3.org/TR/2026/CRD-secure-payment-confirmation-20260702/", title: "Secure Payment Confirmation" }
  - { resource: "https://github.com/duo-labs/py_webauthn", title: "py_webauthn (BSD-3)" }
  - { resource: "https://github.com/Yubico/python-fido2", title: "python-fido2 (BSD-2)" }
  - { resource: "https://github.com/MasterKale/SimpleWebAuthn", title: "SimpleWebAuthn (MIT)" }
  - { resource: "https://privacyidea.readthedocs.io/en/latest/tokens/tokentypes/push.html", title: "privacyIDEA push" }
  - { resource: "https://github.com/binwiederhier/ntfy", title: "ntfy" }
---

# Problem

ameesh v1 signs the owner's authority with an Ed25519 key in a
`0600` file on the PC. The agents run under the same Unix user:
they can read that file. The proof is worthless as long as the key is within
their reach.

# Recommendation

**WebAuthn (passkey) as the default open source mechanism**, under three
conditions: the approval page opens **on the phone**, never on the
agents' PC; the challenge **is** the digest of the canonical receipt; user
verification (UV) is mandatory.

| Level | Authenticator | Requirement |
|---|---|---|
| standard | phone passkey (synced via iCloud/Google, BE=1) | UV |
| high | hardware FIDO key (YubiKey over NFC) | verified attestation (FIDO MDS), BE=0 |

Later: a small native app (Android Keystore/StrongBox, Secure
Enclave) that displays the object itself and signs in ES256 — the only true
"what you see is what you sign". This is the model of Nexlink's device
keys.

# Proposed receipt

```
challenge = SHA-256("ameesh-approval/1\0" || JCS(request))     # JCS = RFC 8785
request   = { v, rp_id, approver, action, decision: approve|deny,
              subject: { type, locator, digest: "sha256:…" },
              params, summary_digest, requested_by, nonce, iat, exp }
receipt   = { v: "ameesh-receipt/1", request, credential_id,
              webauthn: { authenticatorData, clientDataJSON, signature, userHandle } }
```

Verification: recompute the challenge; check `type`, allowed origin,
`crossOrigin` false; `rpIdHash`, UP=1, UV=1 (BE=0 at the high level); look up
the key in a **trust registry** that agents cannot modify;
verify the signature; apply the policy (authorised role); expiry;
atomic consumption of the nonce; recomputation of the artefact's digest at
execution time. `py_webauthn` does this offline.

# What exists

No open source tool found produces a receipt signed by the human, bound to
an artefact's digest and verifiable offline. privacyIDEA (the phone
signs only `challenge|serial`, not the text), Keycloak CIBA (a token, not a
receipt), Authelia/authentik (second factor), ntfy (notification without
signature), Approva and Lelu (agent approvals without cryptographic
binding). These are transports or building blocks, not the solution.

# Building block to create: ameesh-approve

Standalone service (Python, py_webauthn or fido2), **on a host or a
Unix user separate from the agents**:

1. ameesh submits `request`;
2. the service **rebuilds the summary itself** from the artefact (git diff by
   SHA, amount…), never from the agent's text;
3. notification to the phone (ntfy, Matrix, email, Nexlink) with a
   single-use link;
4. the page displays action, summary, short digest; the JS recomputes the
   challenge;
5. `navigator.credentials.get`, `userVerification: "required"`;
6. receipt returned, verified and consumed by ameesh.

# Threats and countermeasures

| Threat | Countermeasure |
|---|---|
| an agent drives the PC's browser (computer use, CDP) and triggers hybrid approval (QR code) | enrol only phone or hardware authenticators; reject requests originating on the PC; "never scan an approval QR code" |
| CDP virtual authenticator, fake enrolment | trust registry modifiable only by out-of-band ceremony (administrator hardware key, or reviewed PR of the canon); attestation at the high level |
| forged summary, compromised server | rendering computed from the source, `summary_digest` in the receipt, isolated service; residual risk removed by the native app |
| replay | nonce, short `exp`, atomic consumption, digest recomputation at execution |
| compromised iCloud/Google account | high level: BE=0, hardware key |

# Not verified

Ed25519 over PIV/OpenPGP depending on YubiKey firmware; device-bound
passkeys on Android; licences of pi-authenticator and authentik;
SPC on iOS; internals of Approva and Lelu.
