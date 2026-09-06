FROM ubuntu@sha256:33ceb71981b602c1a7443a53469e4dba065f7503eab3078a2d7a57a2ab987517

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        git \
        python3 \
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

RUN mkdir -p /home/reviewer /workspace \
    && chmod 0755 /home/reviewer /workspace

ENV HOME=/home/reviewer
WORKDIR /workspace
