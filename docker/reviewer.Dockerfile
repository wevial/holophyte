FROM ubuntu@sha256:33ceb71981b602c1a7443a53469e4dba065f7503eab3078a2d7a57a2ab987517

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        gcc \
        git \
        libc6-dev \
        python3 \
        python3-pip \
        python3-venv \
        ripgrep \
        unzip \
    && rm -rf /var/lib/apt/lists/*

# Bun is pinned to one release so console `bun test` / `bun run build`
# criteria can be witnessed inside the container. The archive's SHA-256 is
# copied from the release's SHASUMS256.txt; a mismatch fails the build.
ARG BUN_VERSION=1.4.2
ARG BUN_SHA256=36368faef7527875d5ffa52e53cd48021741f2a83eb6208a8dd64068d422a913
RUN set -eu \
    && curl -fsSL -o /tmp/bun-linux-x64.zip \
        "https://github.com/oven-sh/bun/releases/download/bun-v${BUN_VERSION}/bun-linux-x64.zip" \
    && echo "${BUN_SHA256}  /tmp/bun-linux-x64.zip" | sha256sum -c - \
    && mkdir -p /opt/bun/bin \
    && unzip -q -j /tmp/bun-linux-x64.zip 'bun-linux-x64/bun' -d /opt/bun/bin \
    && chmod 0755 /opt/bun/bin/bun \
    && ln -s bun /opt/bun/bin/bunx \
    && rm /tmp/bun-linux-x64.zip
ENV PATH=/opt/bun/bin:$PATH

# Node.js is pinned to one release so a target's `npm ci` and `npx` setup and
# verify commands run inside the container. The tarball's SHA-256 is copied
# from the release's SHASUMS256.txt; a mismatch fails the build.
ARG NODE_VERSION=24.18.0
ARG NODE_SHA256=783130984963db7ba9cbd01089eaf2c2efb055c7c1693c943174b967b3050cb8
RUN set -eu \
    && curl -fsSL -o /tmp/node-linux-x64.tar.gz \
        "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-x64.tar.gz" \
    && echo "${NODE_SHA256}  /tmp/node-linux-x64.tar.gz" | sha256sum -c - \
    && mkdir -p /opt/node \
    && tar -C /opt/node --strip-components=1 -xzf /tmp/node-linux-x64.tar.gz \
    && rm /tmp/node-linux-x64.tar.gz
ENV PATH=/opt/node/bin:$PATH

# Chromium and its headless shell are installed by Playwright's own installer
# at the version both capture projects pin, with the system libraries it
# names, so a project's own Playwright of that version finds them under
# PLAYWRIGHT_BROWSERS_PATH; the container's home is an empty tmpfs. Another
# Playwright version needs a new image.
ARG PLAYWRIGHT_VERSION=1.62.1
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright
RUN set -eu \
    && npx --yes "playwright@${PLAYWRIGHT_VERSION}" install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/* /root/.npm /root/.cache \
    && chmod -R a+rX "${PLAYWRIGHT_BROWSERS_PATH}"

# Go is pinned to one release so a Go target's `go test` criteria can be
# witnessed inside the container. The tarball's SHA-256 is copied from the Go
# downloads page; a mismatch fails the build. GOTOOLCHAIN=local makes a module
# asking for another toolchain fail loudly instead of downloading one, and
# every Go cache lives under the writable reviewer home because root is
# mounted read-only. cgo is on, with gcc installed above, because `go test
# -race` needs it.
ARG GO_TARBALL=go1.26.9.linux-amd64.tar.gz
ARG GO_SHA256=42d158b4d8f7b61ac0a830567c940a86098fb7aac52e467a5ebec03ef5cc2f8d
RUN set -eu \
    && curl -fsSL -o /tmp/go.linux-amd64.tar.gz \
        "https://go.dev/dl/${GO_TARBALL}" \
    && echo "${GO_SHA256}  /tmp/go.linux-amd64.tar.gz" | sha256sum -c - \
    && tar -C /usr/local -xzf /tmp/go.linux-amd64.tar.gz \
    && rm /tmp/go.linux-amd64.tar.gz
ENV PATH=/usr/local/go/bin:$PATH \
    GOTOOLCHAIN=local \
    CGO_ENABLED=1 \
    GOPATH=/home/reviewer/go \
    GOMODCACHE=/home/reviewer/go/pkg/mod \
    GOCACHE=/home/reviewer/.cache/go-build \
    TMPDIR=/home/reviewer/tmp \
    GOTMPDIR=/home/reviewer/tmp

# Ruff is pinned to one release so `ruff check .`, a verify command on
# every factory ticket, runs inside the container instead of being reported
# as missing. The tarball's SHA-256 is the release's published checksum; a
# mismatch fails the build.
ARG RUFF_VERSION=0.16.5
ARG RUFF_SHA256=65b8bae7e43f12a91b71036a52176012b3aefb725d5ae263e2771474110a0983
RUN set -eu \
    && curl -fsSL -o /tmp/ruff.tar.gz \
        "https://github.com/astral-sh/ruff/releases/download/${RUFF_VERSION}/ruff-x86_64-unknown-linux-gnu.tar.gz" \
    && echo "${RUFF_SHA256}  /tmp/ruff.tar.gz" | sha256sum -c - \
    && mkdir -p /opt/ruff/bin \
    && tar -C /opt/ruff/bin --strip-components=1 -xzf /tmp/ruff.tar.gz \
    && chmod 0755 /opt/ruff/bin/ruff \
    && rm /tmp/ruff.tar.gz
ENV PATH=/opt/ruff/bin:$PATH

# The Claude CLI is pinned to one native build, the one the official
# installer fetches for that version; its SHA-256 is the linux-x64 checksum in
# the release's manifest.json, and a mismatch fails the build. The managed
# settings make bypass the default permission mode: an implementer turn has no
# one to ask, and the container is its boundary.
ARG CLAUDE_VERSION=2.1.286
ARG CLAUDE_SHA256=fe503f65c6289d59c23e5b21ae44f03583f997dd33a2cbfc75ab4f96fb8fc73f
RUN set -eu \
    && curl -fsSL -o /tmp/claude \
        "https://downloads.claude.ai/claude-code-releases/${CLAUDE_VERSION}/linux-x64/claude" \
    && echo "${CLAUDE_SHA256}  /tmp/claude" | sha256sum -c - \
    && mkdir -p /opt/claude/bin /etc/claude-code \
    && install -m 0755 /tmp/claude /opt/claude/bin/claude \
    && rm /tmp/claude \
    && printf '%s\n' '{"permissions": {"defaultMode": "bypassPermissions"}}' \
        > /etc/claude-code/managed-settings.json \
    && chmod 0644 /etc/claude-code/managed-settings.json
ENV PATH=/opt/claude/bin:$PATH \
    DISABLE_AUTOUPDATER=1

# tomlkit and mcp are the factory's Python dependencies (`requirements.txt`;
# tomlkit for the daemon's `PUT /config` patch, mcp for `holo mcp` only): the
# reviewer's `python3 -m unittest discover` runs against the versions the
# unit check installs.
ARG TOMLKIT_VERSION=0.15.1
ARG MCP_VERSION=2.3.0
RUN python3 -m pip install --no-cache-dir --break-system-packages \
        "tomlkit==${TOMLKIT_VERSION}" \
        "mcp==${MCP_VERSION}"

RUN mkdir -p /home/reviewer /workspace \
    && chmod 0755 /home/reviewer /workspace

ENV HOME=/home/reviewer
WORKDIR /workspace
