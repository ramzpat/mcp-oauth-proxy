# Deploying a new MCP server: template & instructions

This repo deploys **one MCP server per branch**, never from `main`. `main`
carries only the proxy itself (`app.py`, `Dockerfile`) and this template —
each MCP server gets its own `deploy/<name>` branch with its own GitHub
Actions workflow, triggered only by pushes to that branch, configured from
its own GitHub Environment. That keeps unrelated MCP deployments from
triggering each other's CI and lets each one carry completely different
image/command/credentials without conditionals in a shared workflow.

Config is **GitHub Environment vars + secrets only** — no Google Secret
Manager. Every MCP server has different credentials, and keeping all of
them in GitHub keeps setup self-contained to one place instead of split
across two systems. The trade-off: secret values passed via
`--set-env-vars` become visible in plaintext on the Cloud Run revision to
anyone with read access to the service in the GCP console (unlike
Secret Manager references, which show only the secret *name*). If that's
unacceptable for a given MCP server, swap the relevant `--set-env-vars`
entries for `--set-secrets=KEY=secret-name:latest` in the template below
and manage those secrets in Secret Manager instead — the two approaches
are commonly mixed as needed.

## 0. One-time GCP setup (per GCP project, not per MCP)

If this project already deploys another MCP server, skip straight to
step 1 — this is shared infrastructure.

**Artifact Registry (GAR)** — one Docker repo holds every MCP's proxy
image:

```bash
gcloud artifacts repositories create mcp \
  --repository-format=docker --location="$REGION" --project="$PROJECT_ID"
```

**Workload Identity Federation (WIF)** — lets GitHub Actions authenticate
to GCP without a long-lived JSON key:

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

# Deployer needs to build/push images and deploy Cloud Run services:
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:gh-deployer@${PROJECT_ID}.iam.gserviceaccount.com" \
  --role=roles/run.admin
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:gh-deployer@${PROJECT_ID}.iam.gserviceaccount.com" \
  --role=roles/iam.serviceAccountUser
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:gh-deployer@${PROJECT_ID}.iam.gserviceaccount.com" \
  --role=roles/artifactregistry.writer
```

Note the WIF provider's full resource name — you'll need it per MCP branch
below:

```
projects/<PROJECT_NUMBER>/locations/global/workloadIdentityPools/github-pool/providers/github-provider
```

**Google OAuth client** (if using Google as the upstream IdP — one client
can be shared across MCP servers, or create one per server):
Google Cloud Console → APIs & Services → Credentials → OAuth client ID
(Web application). You won't know the exact redirect URI until after the
first deploy of each MCP server — see step 5.

## 1. Create the branch for this MCP

```bash
git checkout main
git checkout -b deploy/<mcp-name>
```

## 2. Copy the template workflow in

```bash
mkdir -p .github/workflows
cp deploy-template/workflow.yml.template .github/workflows/deploy.yml
```

Fill in every `TODO` in that file: the branch name in `on.push.branches`,
the `environment:` name, and the backend container's image/command/port/env
vars for this specific MCP server.

## 3. Create the GitHub Environment

Repo Settings → Environments → New environment, named `<mcp-name>` (match
whatever you put in the workflow's `environment:` field).

**Variables** (Environment → Variables):

| Name | Meaning |
|---|---|
| `PROJECT_ID` | GCP project ID |
| `REGION` | Cloud Run / Artifact Registry region, e.g. `asia-southeast1` |
| `SERVICE` | Cloud Run service name |
| `AR_REPO` | Artifact Registry repo, e.g. `mcp` |
| `WORKLOAD_IDENTITY_PROVIDER` | the WIF provider resource name from step 0 |
| `DEPLOYER_SERVICE_ACCOUNT` | `gh-deployer@<project>.iam.gserviceaccount.com` |
| `ALLOWED_DOMAINS` / `ALLOWED_EMAILS` | proxy allowlist |
| any non-secret env var the backend MCP server needs | e.g. `GRAFANA_URL` |

**Secrets** (Environment → Secrets):

| Name | Meaning |
|---|---|
| `SIGNING_KEY` | `openssl rand -base64 48` — the proxy's JWT signing key |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | upstream OAuth client |
| any credential the backend MCP server needs | e.g. a service-account token |

## 4. Push

```bash
git add .github/workflows/deploy.yml
git commit -m "Add deploy workflow for <mcp-name>"
git push -u origin deploy/<mcp-name>
```

## 5. The public URL is only known after the first deploy

`PUBLIC_URL` (and therefore the OAuth redirect URI and the MCP `RESOURCE`
URI) is whatever `*.run.app` URL Cloud Run happens to assign this service —
which doesn't exist until the service is created. The template workflow
handles this automatically:

- **First run** on a brand-new service: deploys once with a placeholder
  `PUBLIC_URL` just to provision the service and learn its real URL, then
  immediately redeploys with the real URL. The job log prints the OAuth
  redirect URI to register once this finishes — **add it in your IdP's
  console before trying to log in**, since it isn't known any earlier.
- **Every run after that**: the service already exists, so its URL is
  already known and stable — one deploy, no placeholder step.

## 6. Repeat per MCP server

Each new MCP server = a new `deploy/<name>` branch + a new GitHub
Environment, following steps 1–5 again. `main` never changes for this.
