# Serves the bench rig over HTTP: git worktrees, landings and feedback
# over MCP. Build: make image (buildx arm64 → ghcr.io/ckopsa/waymark-bench:<tag>).
FROM python:3.11-slim-bookworm

# git is the rig's one tool; ca-certificates lets it clone over https.
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# clj-kondo lets check name the errors a balance walk misses.
ARG CLJ_KONDO_VERSION=2025.01.16
ARG TARGETARCH
RUN arch=$([ "$TARGETARCH" = "arm64" ] && echo aarch64 || echo amd64) \
 && python -c "import io, sys, urllib.request, zipfile; zipfile.ZipFile(io.BytesIO(urllib.request.urlopen(sys.argv[1]).read())).extract('clj-kondo', '/usr/local/bin')" \
    "https://github.com/clj-kondo/clj-kondo/releases/download/v${CLJ_KONDO_VERSION}/clj-kondo-${CLJ_KONDO_VERSION}-linux-${arch}.zip" \
 && chmod +x /usr/local/bin/clj-kondo \
 && clj-kondo --version

WORKDIR /app

# Dependency layer: the project file and the lock first, so a
# source-only change never re-downloads the one dependency.
COPY pyproject.toml uv.lock README.md ./
RUN pip install --no-cache-dir "pydantic-settings>=2.0"

# The rig itself, installed as the `bench` command.
COPY bench/ bench/
RUN pip install --no-cache-dir --no-deps .

# The configuration inside the image holds the data directory only.
# Repositories come from the engine through `enroll` and persist in
# /data/repos.json, so a container has nothing to hand-edit. Mount a
# volume at /data: it holds the bare clones, the worktrees and the
# landings, and a container that loses it clones again.
RUN mkdir -p /etc/bench /data \
 && printf '{"data_dir": "/data"}\n' > /etc/bench/bench.json
VOLUME /data

# The rig binds every interface inside the container; the job publishes
# the port. The credentials arrive from the job's environment:
# BENCH_GIT_TOKEN for the clone, the push and the landing, and
# BENCH_GITHUB_TOKEN when feedback reads pull requests and pipelines.
ENV BENCH_HOST=0.0.0.0 \
    BENCH_CONFIG=/etc/bench/bench.json \
    PYTHONUNBUFFERED=1
EXPOSE 8101

# The rig runs as root, as the engine's image does: a home cluster's
# host volume is owned by whoever made it, and the container holds
# clones of public repositories and nothing else.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8101/health', timeout=4).status == 200 else 1)"

CMD ["bench", "--http", "8101", "--config", "/etc/bench/bench.json"]
