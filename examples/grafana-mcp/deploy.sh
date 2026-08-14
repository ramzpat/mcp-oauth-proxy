#!/usr/bin/env bash
# Deploy grafana-mcp behind our stateless oauth-proxy on Cloud Run,
# scale-to-zero, using the default *.run.app URL (no custom domain).
#
# Zero external dependencies for statelessness: no database. The only
# shared state is SIGNING_KEY (Secret Manager) -- every OAuth flow
# artifact (client_id, upstream state, auth code, access/refresh tokens)
# is a signed JWT any cold instance can validate on its own.
#
# Two-phase because PUBLIC_URL/RESOURCE must match the assigned run.app
# URL exactly (path included), but that URL isn't known until the service
# is provisioned:
#   1. Deploy once with a placeholder PUBLIC_URL to learn the real URL.
#   2. Re-deploy with PUBLIC_URL/RESOURCE set correctly.

set -euo pipefail

: "${PROJECT_ID:?set PROJECT_ID}"
: "${REGION:?set REGION}"
SERVICE="${SERVICE:-grafana-mcp}"
: "${PROXY_IMAGE:?set PROXY_IMAGE, e.g. \$REGION-docker.pkg.dev/\$PROJECT_ID/mcp/oauth-proxy:latest (build & push first, see README)}"

deploy() {
  local public_url="$1"
  gcloud run deploy "$SERVICE" \
    --project="$PROJECT_ID" \
    --region="$REGION" \
    --allow-unauthenticated \
    --min-instances=0 --max-instances=3 \
    --no-cpu-throttling \
    --container=grafana-mcp \
      --image=grafana/mcp-grafana:latest \
      --command=-t,streamable-http,--allowed-hosts,127.0.0.1,--allowed-origins,127.0.0.1 \
      --set-env-vars="GRAFANA_URL=https://monitoring.pattaravut.info,PORT=8000,HOST=127.0.0.1" \
      --set-secrets="GRAFANA_SERVICE_ACCOUNT_TOKEN=grafana-mcp-token:latest" \
    --container=oauth-proxy \
      --image="$PROXY_IMAGE" \
      --port=8080 \
      --startup-cpu-boost \
      --depends-on=grafana-mcp \
      --set-env-vars="PUBLIC_URL=${public_url},UPSTREAM_URL=http://127.0.0.1:8000,RESOURCE=${public_url}/mcp,ALLOWED_DOMAINS=pattaravut.info" \
      --set-secrets="SIGNING_KEY=mcp-signing-key:latest,GOOGLE_CLIENT_ID=google-client-id:latest,GOOGLE_CLIENT_SECRET=google-client-secret:latest"
}

echo "== Phase 1: provisioning service to learn its run.app URL =="
deploy "https://placeholder.invalid"

SERVICE_URL=$(gcloud run services describe "$SERVICE" \
  --project="$PROJECT_ID" --region="$REGION" \
  --format='value(status.url)')

echo "== Phase 2: re-deploying with PUBLIC_URL=${SERVICE_URL} =="
deploy "$SERVICE_URL"

echo
echo "Done."
echo "MCP endpoint:                          ${SERVICE_URL}/mcp"
echo "Google OAuth redirect URI to register:  ${SERVICE_URL}/oauth2/callback"
echo "(Add that redirect URI in Google Cloud Console BEFORE trying to log in.)"
