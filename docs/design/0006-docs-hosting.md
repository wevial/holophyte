# Docs on Cloudflare Workers

**Status:** accepted · 2026-09-04 · amended 2026-09-05, 2026-10-21

## Context

The manual is an MkDocs Material site built from `docs/`. GitHub Pages was
the first deploy target; Ko prefers Cloudflare or Vercel. Cloudflare Pages
was the plan, but the Cloudflare dashboard now creates new projects as
Workers with static assets, which serves a built site the same way.

## Decision

A Cloudflare Worker (`holophyte-docs`) built by Workers Builds from the
repository: build `pip install mkdocs-material && mkdocs build`, deploy
`npx wrangler deploy` with `wrangler.jsonc` naming `site/` as the assets
directory. Custom domain on the zone already in use. The GitHub workflow
builds with `--strict` as a check and deploys nothing, so no secrets live in
GitHub. Ruff configuration lives in `ruff.toml`, not `pyproject.toml`,
because the build image treats a `pyproject.toml` as a Python package to
install.

The manual's canonical address is https://docs.holophyte.ai/ (`site_url`);
the earlier address stays attached to the same Worker as an alias. The
landing page is a second Worker (`holophyte-landing`) from `landing/`: static
assets in `landing/public/`, no build step, and `landing/wrangler.jsonc`
claiming https://holophyte.ai as its custom domain. Its Workers Build uses
`landing` as the root directory. Its media stays out of the repository, in the R2
bucket `holophyte-media` served at https://media.holophyte.ai/demo/.

## Tickets

Done by hand 2026-09-04 (f794fd7) and 2026-09-05 (cdba854, f4eec67); the
Workers, their builds and the docs domains are operator steps. Live at
https://holophyte.ai/ and https://docs.holophyte.ai/, rebuilt on every push
to `main`.
