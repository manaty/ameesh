---
type: Decision
title: "Several canons on the same host"
description: "ameesh reads a list of canons; a canon is identified by its federation; each synchronisation only touches its own canon; global names go to the first declarer; humans and host described by each canon; ameesh scope declarable in a shared canon."
status: stable
tags: [canon, federation, multi-canon]
decided_by: human:smichea
decision_date: 2026-10-07
attestation: "need confirmed by the owner (\"yes very important\") in his conversation with claude3; terms settled by claude3 under the owner's explicit delegation (\"decide for me\", 2026-10-08), after a night of operation on two canons; unsigned"
generated: { by: "claude3/claude-opus-5-5", at: "2026-10-07T15:40:00+02:00" }
---

# Decision

ameesh manages several simultaneous canons (manaty and Acme). The
terms and the L42–L45 lot plan are in the study
[several canons](../etudes/plusieurs-canons.md).

1. The host configuration carries a list of canons; the first is the
   default canon.
2. A canon is identified by the `id` of its federation. References, the
   registry, packages, the canon state and the authenticator journal
   carry this canon.
3. Each synchronisation only touches its own canon, and closing is done per
   canon.
4. Names stay global: the first canon that declares a name keeps it, and
   a released name can change canon.
5. Humans are resolved in the agent's canon; each canon describes
   the host it uses, and the runner applies the strictest physical
   limits.
6. A shared canon can confine the ameesh cards to a directory
   (`extensions: {ameesh: {scope: …}}` in `federation.yaml`, the only free
   key of the OKF Federation schema — form corrected by L47).

# Status

Decision `stable` since 2026-10-08. The terms (points 4 to 6) were
confirmed as is: they have been running in production on two canons since
the deployment of L36–L47, with no name conflict and no synchronisation
overflow.
