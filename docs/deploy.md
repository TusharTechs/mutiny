# Deploying the demo

The server is the same engine the CLI runs. Nothing here is deployment-only:
`api/index.py` imports `app.server:app`, which is what `uvicorn` serves locally.

## Why not git

Vercel's Python runtime has no `git` binary, so `mutiny/fetch.py` — which shells
out to `git clone` — cannot run there. `mutiny/remote.py` reads the same
repositories over HTTPS instead: tarballs for the trees, the GitHub API for a
pull request's merge base, and `difflib` in place of `git diff`. A test asserts
the two produce identical results.

## Environment

Set these in the Vercel dashboard (Settings → Environment Variables), not in the
repository:

    NEBIUS_API_KEY        required
    NEBIUS_PROJECT_ID     required, for Sandboxes

Optional:

    MUTINY_BUDGET_USD     total the deployment may spend (default 15)
    MUTINY_RUN_CAP_USD    ceiling for one run (default 0.25; a run costs ~0.005)
    MUTINY_RUNS_PER_HOUR  per-address rate limit (default 20)
    GITHUB_TOKEN          raises the GitHub API rate limit; no write scope needed

    UPSTASH_REDIS_REST_URL
    UPSTASH_REDIS_REST_TOKEN

The last two matter more than they look. A serverless deployment has no memory
between requests, so without a shared store the total-spend counter resets
whenever a new instance starts and only the per-run cap is really enforced. With
them, the ceiling is real. Upstash's free tier is enough and needs no card.

### Turning the ceiling on

1. Create a free Redis database at **console.upstash.com** — any region; pick the
   one nearest the deployment.
2. On the database page, copy `UPSTASH_REDIS_REST_URL` and
   `UPSTASH_REDIS_REST_TOKEN` from the **REST API** panel.
3. Add both in Vercel → Settings → Environment Variables, then redeploy.

Confirm it took effect rather than assuming it did:

    curl -s https://<your-deployment>/api/health

`cap_enforced` is `true` only when the shared counter actually answers. A cap
that quietly does nothing is worse than no cap, because it is believed.

Two details worth knowing:

- **A run is charged before it starts**, then corrected once its real cost is
  known. Cost is only knowable at the end, and a counter updated only then lets
  any number of simultaneous runs read the same total and all pass the check.
- **A store that is unreachable does not take the demo down.** The per-run cap
  and the per-address rate limit still apply, and `cap_enforced` reports false.

## Deploying

Import the repository at vercel.com/new. Vercel detects the entrypoint named by
`tool.vercel.entrypoint` in `pyproject.toml` (`main:app`) and routes every
request to the ASGI app, static files included. Set the environment variables in
the import form before the first build, so it comes up working.

Or from a terminal:

    vercel login
    vercel --prod

## Why `pyproject.toml` matters here

Vercel reads dependencies from `pyproject.toml` as readily as from
`requirements.txt`. `fastapi` was in an optional extra and `contree-sdk` was not
declared at all — a build that installed from `pyproject.toml` would have come
up missing both. Both files now pin the same set, so either path produces a
working deployment. There is a test for the engine; this one is checked by
installing the project into a clean environment and loading `main:app`.
