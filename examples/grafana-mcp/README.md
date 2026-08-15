# oauth-proxy for grafana-mcp on Cloud Run (scale-to-zero, run.app URL)

Deploys `grafana/mcp-grafana:latest` behind the stateless `oauth-proxy`
from this repo, using Cloud Run's default `*.run.app` URL — no custom
domain / domain mapping required.

## 1. Build & push

```bash
export PROJECT_ID=your-project
export REGION=asia-southeast1
export REPO=mcp

gcloud artifacts repositories create $REPO --repository-format=docker --location=$REGION 2>/dev/null || true

docker build -t $REGION-docker.pkg.dev/$PROJECT_ID/$REPO/oauth-proxy:latest ../..
docker push $REGION-docker.pkg.dev/$PROJECT_ID/$REPO/oauth-proxy:latest
```

## 2. Secrets

```bash
export PROJECT_ID=your-project
export GRAFANA_SA_TOKEN=...
export GOOGLE_CLIENT_ID=...
export GOOGLE_CLIENT_SECRET=...
./secrets-setup.sh
```

The only thing this creates that isn't a plain credential is
`mcp-signing-key` — a random secret, generated once. It's the entire
"database": every OAuth flow artifact (client_id, upstream state, auth
code, access/refresh tokens) is a JWT signed with it, so any cold Cloud
Run instance can validate/mint one without talking to a shared store.

## 3. Deploy (scale-to-zero, no custom domain)

```bash
export REGION=asia-southeast1
export PROXY_IMAGE=$REGION-docker.pkg.dev/$PROJECT_ID/mcp/oauth-proxy:latest
./deploy.sh
```

`deploy.sh` is two-phase because `PUBLIC_URL`/`RESOURCE` must exactly
match the assigned `*.run.app` URL (path included), but that URL isn't
known until the service exists:

1. Deploys once with a placeholder `PUBLIC_URL` to provision the service
   and learn its real `run.app` URL.
2. Re-deploys (same service, new revision, URL doesn't change) with
   `PUBLIC_URL`/`RESOURCE` set correctly.

At the end it prints the MCP endpoint and the exact Google OAuth redirect
URI to register — **add that redirect URI in Google Cloud Console before
trying to log in**, since it isn't known until phase 1 finishes:

```
MCP endpoint:                          https://grafana-mcp-<hash>-<region>.a.run.app/mcp
Google OAuth redirect URI to register:  https://grafana-mcp-<hash>-<region>.a.run.app/oauth2/callback
```

### One-time Google OAuth client

Google Cloud Console → APIs & Services → Credentials → OAuth client ID
(Web application). You can add/edit the redirect URI on an existing
client any time — create the client now with a placeholder redirect URI,
come back and fill in the real one after phase 1 prints it.

- Consent screen: set to **Internal** if everyone is on your Workspace
  domain — sidesteps Google's verification review entirely.

## Why this is safe to scale to zero

There is no session state anywhere:

- `client_id` from `/register` = a signed JWT of the redirect_uris.
- The `state` round-tripped through Google = a signed JWT carrying the
  entire in-flight authorization request (including this proxy's own
  upstream PKCE verifier).
- The authorization code and the access/refresh tokens are signed JWTs.

Any instance that gets spun up cold can validate/mint any of these, because
validation only needs `SIGNING_KEY` (from Secret Manager) — not shared
in-memory state, and no database. That's what makes `--min-instances=0`
safe here.

Trade-offs to accept:
- First request after idle pays a cold start (~1-2s for this small image)
  on top of the OAuth redirect dance — only affects the *login* step;
  once a user has a refresh token, subsequent tool calls just need
  `/token` (refresh) + `/mcp`, both fast, single-request round trips.
- `--no-cpu-throttling` (CPU always allocated, not just during requests)
  and `--startup-cpu-boost` keep the cold start itself as short as
  possible — worth keeping even at zero min instances.
- No token revocation before expiry (JWTs are self-contained) — that's
  why `ACCESS_TOKEN_TTL` defaults to 1h. Deleting a user from
  `ALLOWED_DOMAINS`/`ALLOWED_EMAILS` and redeploying blocks new logins and
  refreshes immediately, but an already-issued access token remains valid
  until it expires.

## CI/CD: GitHub Actions

[`.github/workflows/deploy-grafana-mcp.yml`](../../.github/workflows/deploy-grafana-mcp.yml)
builds and deploys this service automatically on push to `main` (paths:
`app.py`, `requirements.txt`, `Dockerfile`, `examples/grafana-mcp/**`), or
on demand via `workflow_dispatch`. It runs the same two-phase `gcloud run
deploy` as `deploy.sh` above (skipping straight to a single deploy once the
service already exists, since the `run.app` URL is then already known).

It assumes steps 2 (Secrets) and the one-time Google OAuth client setup
above have already been done manually, and reads everything else from a
GitHub **Environment** named `grafana-mcp`:

| Environment variable | Required | Meaning |
|---|---|---|
| `PROJECT_ID` | yes | GCP project ID |
| `REGION` | yes | Cloud Run / Artifact Registry region, e.g. `asia-southeast1` |
| `WORKLOAD_IDENTITY_PROVIDER` | yes | Full WIF provider resource name for [`google-github-actions/auth`](https://github.com/google-github-actions/auth) |
| `DEPLOYER_SERVICE_ACCOUNT` | yes | Service account email the workflow impersonates via WIF; needs `roles/run.admin`, `roles/iam.serviceAccountUser`, and `roles/artifactregistry.writer` on the project, plus `roles/secretmanager.secretAccessor` on the four secrets from step 2 |
| `SERVICE` | no | Cloud Run service name, default `grafana-mcp` |
| `AR_REPO` | no | Artifact Registry repo name, default `mcp` |
| `GRAFANA_URL` | no | Grafana instance URL, default `https://monitoring.pattaravut.info` |
| `ALLOWED_DOMAINS` | no | Comma-separated allowed Workspace domains, default `pattaravut.info` |

No GitHub **secrets** are needed — the sensitive values
(`GRAFANA_SERVICE_ACCOUNT_TOKEN`, `SIGNING_KEY`, `GOOGLE_CLIENT_ID`,
`GOOGLE_CLIENT_SECRET`) live in Secret Manager (via `secrets-setup.sh`) and
are wired in by name with `--set-secrets`; the workflow only needs GCP
credentials to deploy.

Set up Workload Identity Federation once, e.g.:

```bash
gcloud iam workload-identity-pools create github-pool \
  --project="$PROJECT_ID" --location=global --display-name="GitHub Actions"

gcloud iam workload-identity-pools providers create-oidc github-provider \
  --project="$PROJECT_ID" --location=global --workload-identity-pool=github-pool \
  --display-name="GitHub" --issuer-uri="https://token.actions.githubusercontent.com" \
  --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository" \
  --attribute-condition="assertion.repository=='<owner>/mcp-oauth-proxy'"

gcloud iam service-accounts create gh-deployer --project="$PROJECT_ID"

gcloud iam service-accounts add-iam-policy-binding \
  "gh-deployer@${PROJECT_ID}.iam.gserviceaccount.com" \
  --project="$PROJECT_ID" --role=roles/iam.workloadIdentityUser \
  --member="principalSet://iam.googleapis.com/projects/<PROJECT_NUMBER>/locations/global/workloadIdentityPools/github-pool/attribute.repository/<owner>/mcp-oauth-proxy"
```

Then grant `gh-deployer@$PROJECT_ID.iam.gserviceaccount.com` the roles
listed above, and set `WORKLOAD_IDENTITY_PROVIDER` to
`projects/<PROJECT_NUMBER>/locations/global/workloadIdentityPools/github-pool/providers/github-provider`.

## Original docker-compose

See [`docker-compose.original.yml`](docker-compose.original.yml) — the
local-only compose file this deployment was ported from. Not used for the
Cloud Run deploy itself, since the proxy replaces the need to expose
`grafana-mcp`'s port directly.
