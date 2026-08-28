FROM ubuntu@sha256:33ceb71981b602c1a7443a53469e4dba065f7503eab3078a2d7a57a2ab987517

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        ca-certificates \
        git \
        python3 \
        ripgrep \
    && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /home/reviewer /workspace \
    && chmod 0755 /home/reviewer /workspace

ENV HOME=/home/reviewer
WORKDIR /workspace
