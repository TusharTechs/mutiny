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

## Deploying

    vercel login
    vercel --prod

The first run asks which scope and project to use. Static files are served by
the function itself: `vercel.json` rewrites every path to it.
