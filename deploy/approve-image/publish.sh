#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
#
# #953 (R5-IMAGE-APPROVE) : publication de l'image approve sur ghcr.io.
# Appele par .github/workflows/image-approve.yml, uniquement depuis main apres
# la fusion et le feu du pilote (aucune publication avant fusion) ; politique
# complete dans docs/IMAGE-APPROVE.md. L'image locale (construite par le
# workflow, taguee sha-<12>) n'est qu'une source possible du tag de revision.
#
# Deux invariants, verrouilles par tests/test_l953_image_approve.py :
#
#  1. Une absence de tag ne se conclut que sur un 404 du registre. Toute autre
#     reponse (reseau, 401/403, 5xx) arrete le run avant la moindre ecriture :
#     un lookup transitoirement en echec ne peut pas faire reecrire un tag
#     existant. Le tag sha-<12> n'est pousse que si son absence est prouvee.
#
#  2. Le tag de version n'est jamais pousse depuis l'image locale. Il est cree
#     en recopiant le manifeste distant du tag sha-<12> (meme octets, donc meme
#     digest) : si le tag sha existe deja avec le digest D1 et que la
#     reconstruction locale donne D2, c'est D1 qui est promu. Le digest source
#     est lu et verifie avant la mutation, et la version deja publiee sur un
#     autre digest arrete le run sans rien ecrire.
#
# Entrees (environnement) : IMAGE, SHA_TAG, VERSION, GHCR_USER, GHCR_TOKEN ;
# REVISION est facultative (resume). Sortie : digest.txt et le resume du run
# (GITHUB_STEP_SUMMARY).
set -euo pipefail

: "${IMAGE:?IMAGE manquant}" "${SHA_TAG:?SHA_TAG manquant}" \
  "${VERSION:?VERSION manquante}" \
  "${GHCR_USER:?GHCR_USER manquant}" "${GHCR_TOKEN:?GHCR_TOKEN manquant}"

printf '%s' "$SHA_TAG" | grep -Eq '^sha-[0-9a-f]{12}$' \
  || { printf '::error::tag de revision inattendu : %s\n' "$SHA_TAG" >&2; exit 1; }
printf '%s' "$VERSION" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+([-.+][0-9A-Za-z.-]+)?$' \
  || { printf '::error::version inattendue : %s\n' "$VERSION" >&2; exit 1; }

registry="${IMAGE%%/*}"
repo="${IMAGE#*/}"
api="https://$registry/v2/$repo"
accept='application/vnd.oci.image.manifest.v1+json, application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.v2+json, application/vnd.docker.distribution.manifest.list.v2+json'

work="$(mktemp -d "${RUNNER_TEMP:-${TMPDIR:-/tmp}}/ameesh-approve-publish.XXXXXX")"
cleanup() {
  rm -rf "$work"
  docker logout "$registry" >/dev/null 2>&1 || true
}
trap cleanup EXIT

die() { printf '::error::%s\n' "$*" >&2; exit 1; }

# Jeton bearer du registre (le meme echange que `docker login`).
token_json="$(curl -fsS -u "$GHCR_USER:$GHCR_TOKEN" --get \
    --data-urlencode "service=$registry" \
    --data-urlencode "scope=repository:$repo:pull,push" \
    "https://$registry/token")" \
  || die "jeton de registre non obtenu : identifiants refuses ou registre injoignable"
token="$(printf '%s' "$token_json" \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("token") or d.get("access_token") or "")')" \
  || die "reponse de jeton illisible"
[ -n "$token" ] || die "jeton de registre vide"

printf '%s' "$GHCR_TOKEN" | docker login "$registry" -u "$GHCR_USER" --password-stdin >/dev/null \
  || die "docker login refuse"

# lookup <ref> : pose LOOKUP_STATE=present|absent, LOOKUP_DIGEST, LOOKUP_TYPE,
# et laisse le corps du manifeste dans $work/body. Toute reponse autre qu'un
# 200 (prouve la presence) ou un 404 (prouve l'absence) arrete le run.
lookup() {
  local ref="$1" code header computed
  LOOKUP_STATE=""
  LOOKUP_DIGEST=""
  LOOKUP_TYPE=""
  code="$(curl -sS -o "$work/body" -D "$work/headers" -w '%{http_code}' \
      -H "Authorization: Bearer $token" -H "Accept: $accept" \
      "$api/manifests/$ref" || true)"
  case "${code:-000}" in
    200)
      computed="sha256:$(sha256sum "$work/body" | cut -d' ' -f1)"
      header="$(sed -n 's/^[Dd]ocker-[Cc]ontent-[Dd]igest:[[:space:]]*//p' "$work/headers" \
        | tr -d '\r' | head -1)"
      if [ -n "$header" ] && [ "$header" != "$computed" ]; then
        die "registre incoherent pour $ref : annonce $header, corps $computed"
      fi
      LOOKUP_STATE=present
      LOOKUP_DIGEST="$computed"
      LOOKUP_TYPE="$(sed -n 's/^[Cc]ontent-[Tt]ype:[[:space:]]*//p' "$work/headers" \
        | tr -d '\r' | head -1)"
      [ -n "$LOOKUP_TYPE" ] || LOOKUP_TYPE='application/vnd.oci.image.manifest.v1+json'
      ;;
    404) LOOKUP_STATE=absent ;;
    000) die "registre injoignable pour $ref : rien n'a ete publie" ;;
    *)   die "le registre a repondu $code pour $ref : erreur autre qu'une absence prouvee, rien n'a ete publie" ;;
  esac
}

# --- tag de revision : pousse seulement si le registre prouve son absence ----
lookup "$SHA_TAG"
if [ "$LOOKUP_STATE" = present ]; then
  digest="$LOOKUP_DIGEST"
  echo "tag $SHA_TAG deja publie ($digest) : jamais reecrit, image locale ignoree"
else
  docker push "$IMAGE:$SHA_TAG" | tee "$work/push.log"
  lookup "$SHA_TAG"
  [ "$LOOKUP_STATE" = present ] || die "le registre ne sert pas $SHA_TAG apres le push"
  digest="$LOOKUP_DIGEST"
  echo "tag $SHA_TAG publie : $digest"
fi

# --- tag de version : promotion du manifeste distant, jamais l'image locale ---
published="$SHA_TAG"
lookup "$VERSION"
case "$LOOKUP_STATE" in
  present)
    [ "$LOOKUP_DIGEST" = "$digest" ] \
      || die "$IMAGE:$VERSION pointe deja sur $LOOKUP_DIGEST, pas $digest : incrementer la version (aucune ecriture sur la version)"
    echo "tag $VERSION deja publie avec le meme digest : $LOOKUP_DIGEST"
    ;;
  absent)
    # relire la source juste avant la mutation : c'est ce manifeste, et lui
    # seul, qui est recopie sous le tag de version.
    lookup "$SHA_TAG"
    [ "$LOOKUP_STATE" = present ] || die "tag source $SHA_TAG disparu avant promotion"
    [ "$LOOKUP_DIGEST" = "$digest" ] || die "tag source $SHA_TAG change de digest pendant le run"
    cp "$work/body" "$work/promotion"
    got="sha256:$(sha256sum "$work/promotion" | cut -d' ' -f1)"
    [ "$got" = "$digest" ] || die "manifeste source $got different du digest $digest avant promotion"
    code="$(curl -sS -o "$work/put.out" -D "$work/put.headers" -w '%{http_code}' \
        -X PUT -H "Authorization: Bearer $token" -H "Content-Type: $LOOKUP_TYPE" \
        --data-binary @"$work/promotion" "$api/manifests/$VERSION" || true)"
    case "${code:-000}" in
      200|201) ;;
      *) die "copie du tag $VERSION refusee ($code) : $(head -c 300 "$work/put.out" 2>/dev/null || true)" ;;
    esac
    lookup "$VERSION"
    if [ "$LOOKUP_STATE" != present ] || [ "$LOOKUP_DIGEST" != "$digest" ]; then
      die "$VERSION ne rend pas $digest apres promotion (recu ${LOOKUP_DIGEST:-rien})"
    fi
    echo "tag $VERSION cree par promotion du manifeste distant $digest (aucune image locale poussee)"
    ;;
esac
published="$published, $VERSION"

# --- le registre sert bien le digest annonce (preuve de disponibilite) -------
servi=0
for attempt in 1 2 3 4 5; do
  lookup "$digest"
  if [ "$LOOKUP_STATE" = present ] && [ "$LOOKUP_DIGEST" = "$digest" ]; then
    servi=1
    break
  fi
  sleep 3
done
[ "$servi" = 1 ] || die "le registre ne sert pas $IMAGE@$digest apres publication"

{
  echo "image=$IMAGE"
  echo "revision=${REVISION:-unknown}"
  echo "tags=$published"
  echo "digest=$digest"
} | tee digest.txt

if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
  {
    echo "## image approve publiee"
    echo ""
    echo "| champ | valeur |"
    echo "|---|---|"
    echo "| image | \`$IMAGE\` |"
    echo "| tags | \`$published\` |"
    echo "| revision | \`${REVISION:-unknown}\` |"
    echo "| digest | \`$digest\` |"
    echo ""
    echo "Epingler le digest : \`$IMAGE@$digest\` — jamais un tag mobile."
  } >> "$GITHUB_STEP_SUMMARY"
fi
