#!/usr/bin/env bash
# One-time setup: Secret Manager entries for our stateless oauth-proxy.
set -euo pipefail

: "${PROJECT_ID:?set PROJECT_ID}"
: "${GRAFANA_SA_TOKEN:?set GRAFANA_SA_TOKEN}"
: "${GOOGLE_CLIENT_ID:?set GOOGLE_CLIENT_ID}"
: "${GOOGLE_CLIENT_SECRET:?set GOOGLE_CLIENT_SECRET}"

create_or_update() {
  local name="$1" value="$2"
  if gcloud secrets describe "$name" --project="$PROJECT_ID" >/dev/null 2>&1; then
    printf '%s' "$value" | gcloud secrets versions add "$name" --project="$PROJECT_ID" --data-file=-
  else
    printf '%s' "$value" | gcloud secrets create "$name" --project="$PROJECT_ID" --data-file=-
  fi
}

create_or_update grafana-mcp-token "$GRAFANA_SA_TOKEN"
create_or_update google-client-id "$GOOGLE_CLIENT_ID"
create_or_update google-client-secret "$GOOGLE_CLIENT_SECRET"

# The ONLY piece of shared state this design needs: the JWT signing key.
# No database -- any cold instance can validate/mint client_ids, auth
# codes, and access/refresh tokens using just this key.
if ! gcloud secrets describe mcp-signing-key --project="$PROJECT_ID" >/dev/null 2>&1; then
  openssl rand -base64 48 | gcloud secrets create mcp-signing-key --project="$PROJECT_ID" --data-file=-
fi

echo "Secrets ready."
