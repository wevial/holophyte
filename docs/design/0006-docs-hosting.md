# Docs on Cloudflare Pages

**Status:** accepted · 2026-09-04

## Context

The manual is an MkDocs Material site built from `docs/`. GitHub Pages was
the first deploy target; Ko prefers Cloudflare or Vercel.

## Decision

Cloudflare Pages connected to the repository (build `pip install
mkdocs-material && mkdocs build`, output `site`), custom domain on the zone
already in use. The GitHub workflow builds with `--strict` as a check and
deploys nothing, so no secrets live in GitHub.

## Tickets

Done by hand 2026-09-04 (f794fd7); the Pages project is an operator step.
