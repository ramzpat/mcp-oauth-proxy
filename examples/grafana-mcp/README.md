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

## The oauth-proxy image

`main`'s [`.github/workflows/build.yml`](../../.github/workflows/build.yml)
builds+publishes the proxy image to GitHub Container Registry on every
push to `main` — one shared, generic image (no MCP-specific config baked
in) that any MCP server's deployment can pull:

```
ghcr.io/ramzpat/mcp-oauth-proxy:latest
```

## CI/CD: GitHub Actions

This branch (`deploy/grafana-mcp`) deploys automatically on every push to
itself, via [`.github/workflows/deploy.yml`](../../.github/workflows/deploy.yml)
— a thin caller of the reusable
[`deploy-service.yml`](https://github.com/ramzpat/mcp-oauth-proxy/blob/main/.github/workflows/deploy-service.yml)
workflow on `main` (see that repo's `README.md` → "Deploying a new MCP
server to Cloud Run" for the full shape and setup walkthrough).

There's no build step on this branch: the image above is generic and
built once on `main`. This workflow just deploys it — no Artifact
Registry, no WIF-authenticated `docker push` here, nothing to build.

The workflow is two jobs:

- `prepare`: composes `GRAFANA_URL`/`GRAFANA_SERVICE_ACCOUNT_TOKEN` into
  the single `backend_env_vars` string the reusable workflow expects —
  needed because a reusable workflow's `with:` can't reference the
  `secrets` context directly, so this MCP's own credential has to be
  turned into a plain job output first.
- `deploy`: calls `deploy-service.yml@main` with this branch's Cloud
  Run/WIF config and that composed string, `secrets: inherit`-ing
  `SIGNING_KEY`/`GOOGLE_CLIENT_ID`/`GOOGLE_CLIENT_SECRET` through
  automatically.

Before the first push, do the one-time GCP setup (Workload Identity
Federation + a deployer service account — no Artifact Registry repo
needed; see the setup walkthrough linked above), then create a GitHub
Environment named **`grafana-mcp`** with:

**Variables:**

| Name | Meaning |
|---|---|
| `PROJECT_ID` | GCP project ID |
| `REGION` | Cloud Run region, e.g. `asia-southeast1` |
| `SERVICE` | Cloud Run service name, e.g. `grafana-mcp` |
| `WORKLOAD_IDENTITY_PROVIDER` | WIF provider resource name from the one-time setup |
| `DEPLOYER_SERVICE_ACCOUNT` | `gh-deployer@<project>.iam.gserviceaccount.com` |
| `ALLOWED_DOMAINS` | Comma-separated allowed Workspace domains, e.g. `pattaravut.info` |
| `GRAFANA_URL` | Your Grafana instance URL, e.g. `https://monitoring.pattaravut.info` |

**Secrets:**

| Name | Meaning |
|---|---|
| `SIGNING_KEY` | `openssl rand -base64 48` — proxy's JWT signing key |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | upstream Google OAuth client |
| `GRAFANA_SERVICE_ACCOUNT_TOKEN` | Grafana service account token for `mcp-grafana` |

These live entirely in this GitHub Environment — no Google Secret Manager
involved. If the `ghcr.io` image is private, Cloud Run also needs a way to
pull it (make the package public, or mirror it via an Artifact Registry
remote repository) — see the setup walkthrough on `main` for details.

**The public `*.run.app` URL isn't known until the first deploy finishes** —
the reusable workflow bootstraps in two phases on a brand-new service
(placeholder URL, then a redeploy with the real one) and prints the OAuth
redirect URI to register at the end. Every push after that is a single,
direct redeploy since the URL is already known.

## Original docker-compose

See [`docker-compose.original.yml`](docker-compose.original.yml) — the
local-only compose file this deployment was ported from. Not used for the
Cloud Run deploy itself, since the proxy replaces the need to expose
`grafana-mcp`'s port directly.
