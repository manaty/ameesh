---
type: Decision
title: "Dropping the Ed25519 ceremony on the PC; human authority through ameesh-approve"
description: "A human's signature is made out of the agents' reach: passkey (WebAuthn), Nexlink app, or native mobile app; a single receipt format."
status: stable
tags: [autorite, signature, ameesh-approve, mobile]
decided_by: human:smichea
decision_date: 2026-10-04
attestation: "reported by mesh-design from its conversation with the owner; unsigned (R13 not yet in service)"
generated: { by: "mesh-design/claude-opus-5-5", at: "2026-10-04T00:50:00+02:00" }
sources:
  - { resource: "../etudes/autorite-humaine.md", title: "Human authority study" }
---

# Context

Step 3 of `BASCULE.md` planned an Ed25519 key of the owner in a
`0600` file on the PC. The agents run under the same Unix user:
they can read that file, and all of them can register an `owner` key in the database.

# Decision

- **Option A**: the Ed25519 key ceremony on the PC is **dropped**.
  A human's authority goes through **ameesh-approve**: the key is never on
  an agent machine; the receipt is bound to the exact fingerprint, with an expiry and
  single use.
- **Mixed population**: among the humans, **some will use Nexlink,
  others will not**. The former approve from the Nexlink app (non-exportable
  device keys); the others with a passkey (WebAuthn page).
- A **native mobile app** may be needed for some organisations: ameesh provides for its interfaces ([0015](0015-interfaces-des-facades.md)).

# Consequences

- ameesh-approve has **several approval front ends** behind **a single receipt
  format** and a single verification: WebAuthn page (open source, by default),
  Nexlink app (native), native mobile app developed by an
  organisation on the public ameesh interfaces (Keystore / Secure
  Enclave; displays the object itself and signs — the best "what you
  see is what you sign").
- The public key registry lives in the OKF canon (reviewed PR) and associates
  each human with one or more authenticators; the policy can require
  a level (native app or hardware key for sensitive actions).
- The v1 Ed25519 code is kept for agent provenance (R13) and
  tests; the receipt model (signed expiry, consumed nonce, fingerprint) is
  taken over by ameesh-approve.
- Change request to be examined after the security freezes: verification
  of WebAuthn/ES256 receipts, federation roles instead of
  `owner`/`agent`, public keys read from the canon, step 3 of `BASCULE.md`
  replaced.
