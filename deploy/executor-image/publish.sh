#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
#
# #3166 : publication de l'image `ameesh-executor` sur ghcr.io, et relevé du
# digest linux/amd64 que le bureau signe (O4, docs/IMAGE-EXECUTEUR.md §9).
# Appelé par .github/workflows/image-executor.yml, uniquement depuis main après
# la fusion et le feu du pilote (aucune publication avant fusion). Le contrat
# complet (registre, tag, digest, architecture, retour arrière) est dans
# docs/IMAGE-EXECUTEUR.md §9.
#
# Quatre invariants, verrouillés par tests/test_l3166_executor_image_publish.py :
#
#  1. Une absence de tag ne se conclut que sur un 404 du registre. Toute autre
#     réponse (réseau, 401/403, 5xx) arrête le run avant la moindre écriture :
#     un lookup transitoirement en échec ne peut pas faire réécrire un tag.
#
#  2. Le tag est immuable et ne porte que la révision (`sha-<12>`). Un tag déjà
#     publié n'est jamais réécrit : la même révision rend le même digest.
#
#  3. Le digest rendu est celui du manifeste linux/amd64 que le bureau signe :
#     l'architecture de l'image locale est vérifiée avant tout réseau, puis le
#     manifeste et le blob de configuration servis par le registre sont relus
#     après le push. Un index multi-architecture est refusé.
#
#  4. Le jeton GHCR et le bearer du registre ne passent jamais par argv : curl
#     les lit dans sa configuration sur stdin (`-K -`) et `docker login` reçoit
#     le mot de passe par `--password-stdin`. Sur l'hôte ameesh-ci, plusieurs
#     runners partagent l'utilisateur : /proc/<pid>/cmdline exposerait un
#     secret passé en ligne de commande.
#
# Entrées (environnement) : IMAGE, SHA_TAG, GHCR_USER, GHCR_TOKEN ; VERSION,
# REVISION et PLATFORM sont facultatives (résumé ; PLATFORM vaut linux/amd64).
# Sortie : digest.txt et le résumé du run (GITHUB_STEP_SUMMARY).
set -euo pipefail

: "${IMAGE:?IMAGE manquant}" "${SHA_TAG:?SHA_TAG manquant}" \
  "${GHCR_USER:?GHCR_USER manquant}" "${GHCR_TOKEN:?GHCR_TOKEN manquant}"

VERSION="${VERSION:-unknown}"
REVISION="${REVISION:-unknown}"
PLATFORM="${PLATFORM:-linux/amd64}"
os="${PLATFORM%%/*}"
arch="${PLATFORM#*/}"
if [ -z "$os" ] || [ -z "$arch" ] || [ "$os" = "$PLATFORM" ]; then
  printf '::error::PLATFORM inattendu : %s (os/architecture attendu)\n' "$PLATFORM" >&2
  exit 1
fi

printf '%s' "$SHA_TAG" | grep -Eq '^sha-[0-9a-f]{12}$' \
  || { printf '::error::tag de revision inattendu : %s\n' "$SHA_TAG" >&2; exit 1; }
if [ "$VERSION" != unknown ]; then
  printf '%s' "$VERSION" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+([-.+][0-9A-Za-z.-]+)?$' \
    || { printf '::error::version inattendue : %s\n' "$VERSION" >&2; exit 1; }
fi

registry="${IMAGE%%/*}"
repo="${IMAGE#*/}"
api="https://$registry/v2/$repo"
accept='application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.v2+json'

work="$(mktemp -d "${RUNNER_TEMP:-${TMPDIR:-/tmp}}/ameesh-executor-publish.XXXXXX")"
cleanup() {
  rm -rf "$work"
  docker logout "$registry" >/dev/null 2>&1 || true
}
trap cleanup EXIT

die() { printf '::error::%s\n' "$*" >&2; exit 1; }

# L'image locale doit être celle que le bureau signera. Ce contrôle est fait
# avant tout réseau : un manifeste d'une autre architecture ne peut pas être
# publié sous le digest linux/amd64 promis.
local_platform="$(docker image inspect --format '{{.Os}}/{{.Architecture}}' \
    "$IMAGE:$SHA_TAG" 2>/dev/null)" \
  || die "image locale absente : $IMAGE:$SHA_TAG"
[ "$local_platform" = "$PLATFORM" ] \
  || die "image locale $local_platform, $PLATFORM attendu : le digest signé doit être celui du manifeste $PLATFORM"

# Les secrets (jeton GHCR, bearer du registre) ne passent JAMAIS par argv :
# /proc/<pid>/cmdline les exposerait à tout processus du même utilisateur, et
# l'hôte ameesh-ci fait tourner plusieurs runners. curl les lit donc dans sa
# configuration sur stdin (`-K -`). Les valeurs sont restreintes à un alphabet
# sûr : une valeur hostile ne peut pas fermer la citation ni injecter une ligne
# de configuration.
config_value() {  # <option curl> <valeur>
  case "$2" in
    ''|*[!A-Za-z0-9._~+/:=\ -]*) die "$1 invalide pour la configuration curl" ;;
  esac
  printf '%s = "%s"\n' "$1" "$2"
}

# Jeton bearer du registre (le même échange que `docker login`).
token_json="$({ config_value user "$GHCR_USER:$GHCR_TOKEN"; } \
  | curl -fsS -K - --get \
      --data-urlencode "service=$registry" \
      --data-urlencode "scope=repository:$repo:pull,push" \
      "https://$registry/token")" \
  || die "jeton de registre non obtenu : identifiants refusés ou registre injoignable"
token="$(printf '%s' "$token_json" \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("token") or d.get("access_token") or "")')" \
  || die "réponse de jeton illisible"
[ -n "$token" ] || die "jeton de registre vide"

printf '%s' "$GHCR_TOKEN" | docker login "$registry" -u "$GHCR_USER" --password-stdin >/dev/null \
  || die "docker login refusé"

# lookup <ref> : pose LOOKUP_STATE=present|absent, LOOKUP_DIGEST, LOOKUP_TYPE,
# et laisse le corps du manifeste dans $work/body. Toute réponse autre qu'un
# 200 (prouve la présence) ou un 404 (prouve l'absence) arrête le run.
lookup() {
  local ref="$1" code header computed
  LOOKUP_STATE=""
  LOOKUP_DIGEST=""
  LOOKUP_TYPE=""
  code="$({ config_value header "Authorization: Bearer $token"; } \
      | curl -sS -K - -o "$work/body" -D "$work/headers" -w '%{http_code}' \
        -H "Accept: $accept" \
        "$api/manifests/$ref" || true)"
  case "${code:-000}" in
    200)
      computed="sha256:$(sha256sum "$work/body" | cut -d' ' -f1)"
      header="$(sed -n 's/^[Dd]ocker-[Cc]ontent-[Dd]igest:[[:space:]]*//p' "$work/headers" \
        | tr -d '\r' | head -1)"
      if [ -n "$header" ] && [ "$header" != "$computed" ]; then
        die "registre incohérent pour $ref : annonce $header, corps $computed"
      fi
      LOOKUP_STATE=present
      LOOKUP_DIGEST="$computed"
      LOOKUP_TYPE="$(sed -n 's/^[Cc]ontent-[Tt]ype:[[:space:]]*//p' "$work/headers" \
        | tr -d '\r' | head -1)"
      [ -n "$LOOKUP_TYPE" ] || LOOKUP_TYPE='application/vnd.oci.image.manifest.v1+json'
      ;;
    404) LOOKUP_STATE=absent ;;
    000) die "registre injoignable pour $ref : rien n'a été publié" ;;
    *)   die "le registre a répondu $code pour $ref : erreur autre qu'une absence prouvée, rien n'a été publié" ;;
  esac
}

# verify_platform : le manifeste servi est un manifeste simple, et son blob de
# configuration dit bien l'OS et l'architecture attendus. C'est cette valeur
# @sha256 que le bureau signe (O4) ; un index multi-architecture n'a pas de config.
verify_platform() {
  local ref="$1" config code
  case "$LOOKUP_TYPE" in
    application/vnd.oci.image.index.v1+json|application/vnd.docker.distribution.manifest.list.v2+json)
      die "le registre sert un index multi-architecture pour $ref : le digest signé doit être celui du manifeste $PLATFORM" ;;
  esac
  config="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["config"]["digest"])' \
      "$work/body" 2>/dev/null)" \
    || die "manifeste $ref illisible : aucun blob de configuration"
  [ -n "$config" ] || die "manifeste $ref sans blob de configuration"
  code="$({ config_value header "Authorization: Bearer $token"; } \
      | curl -sS -K - -o "$work/config" -w '%{http_code}' \
        -H 'Accept: application/octet-stream' \
        "$api/blobs/$config" || true)"
  [ "${code:-000}" = 200 ] || die "blob de configuration $config illisible ($code)"
  local got
  got="$(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print("%s/%s" % (d.get("os",""), d.get("architecture","")))' \
      "$work/config" 2>/dev/null)" \
    || die "blob de configuration $config illisible"
  [ "$got" = "$PLATFORM" ] \
    || die "le registre sert $got pour $ref, $PLATFORM attendu : le digest signé serait celui d'une autre architecture"
}

# --- tag de révision : poussé seulement si le registre prouve son absence ----
lookup "$SHA_TAG"
if [ "$LOOKUP_STATE" = present ]; then
  digest="$LOOKUP_DIGEST"
  echo "tag $SHA_TAG déjà publié ($digest) : jamais réécrit, image locale ignorée"
  verify_platform "$SHA_TAG"
else
  docker push "$IMAGE:$SHA_TAG" | tee "$work/push.log"
  lookup "$SHA_TAG"
  [ "$LOOKUP_STATE" = present ] || die "le registre ne sert pas $SHA_TAG après le push"
  digest="$LOOKUP_DIGEST"
  verify_platform "$SHA_TAG"
  echo "tag $SHA_TAG publié : $digest"
fi

# --- le registre sert bien le digest annoncé (preuve de disponibilité) -------
servi=0
for attempt in 1 2 3 4 5; do
  lookup "$digest"
  if [ "$LOOKUP_STATE" = present ] && [ "$LOOKUP_DIGEST" = "$digest" ]; then
    servi=1
    break
  fi
  sleep 3
done
[ "$servi" = 1 ] || die "le registre ne sert pas $IMAGE@$digest après publication"
verify_platform "$digest"

image_ref="$IMAGE@$digest"
{
  echo "image=$IMAGE"
  echo "revision=$REVISION"
  echo "version=$VERSION"
  echo "tag=$SHA_TAG"
  echo "platform=$PLATFORM"
  echo "digest=$digest"
  echo "image_ref=$image_ref"
} | tee digest.txt

if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
  {
    echo "## image ameesh-executor publiée"
    echo ""
    echo "| champ | valeur |"
    echo "|---|---|"
    echo "| image | \`$IMAGE\` |"
    echo "| tag | \`$SHA_TAG\` |"
    echo "| révision | \`$REVISION\` |"
    echo "| version | \`$VERSION\` |"
    echo "| plateforme | \`$PLATFORM\` |"
    echo "| digest | \`$digest\` |"
    echo ""
    echo "Référence à signer (O4, ES256 P-256, P1363) :"
    echo ""
    echo '```'
    echo "$image_ref"
    echo '```'
    echo ""
    echo "Le bureau rend une signature de 86 caractères : \`deploy/executor-image/sign_release.py sign --key <clé> $image_ref\`."
  } >> "$GITHUB_STEP_SUMMARY"
fi
