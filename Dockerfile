# Serves the bench rig over HTTP: git worktrees, landings and feedback
# over MCP. Build: make image (buildx arm64 → ghcr.io/ckopsa/waymark-bench:<tag>).

# Node's major version, pinned to the one ckopsa/waymark's CI browser
# jobs run: a bump is this one line. .github/workflows/tests.yml reads
# its `node-version` from this line, so keep the form `ARG NODE_MAJOR=<number>`.
ARG NODE_MAJOR=22
FROM node:${NODE_MAJOR}-bookworm-slim AS node

FROM python:3.11-slim-bookworm

# git is the rig's one tool; ca-certificates lets it clone over https;
# curl fetches the Clojure CLI's installer and its tarball.
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*

# node lets check parse the changed .js and .mjs files with
# `node --check`, by the grammar ckopsa/waymark's CI parses them with.
# The build fails when `node --version` names another major version.
ARG NODE_MAJOR
COPY --from=node /usr/local/bin/node /usr/local/bin/node
RUN node --version | grep -E "^v${NODE_MAJOR}\."

# A JDK 21, the Temurin ckopsa/waymark's CI uses, for the check step's
# `clojure -M:check`.
COPY --from=eclipse-temurin:21-jdk /opt/java/openjdk /opt/java/openjdk
ENV JAVA_HOME=/opt/java/openjdk \
    PATH=/opt/java/openjdk/bin:$PATH

# The Clojure CLI, pinned to the version ckopsa/waymark's CI pins: a bump
# is this one line. The build fails when `clojure --version` names another.
ARG CLOJURE_CLI_VERSION=1.12.5.1664
RUN curl -fsSLO "https://github.com/clojure/brew-install/releases/download/${CLOJURE_CLI_VERSION}/linux-install.sh" \
 && bash linux-install.sh \
 && rm linux-install.sh \
 && clojure --version | grep -F "$CLOJURE_CLI_VERSION" \
 && rm -rf /root/.m2 /root/.gitlibs

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
# source-only change never re-downloads the dependencies.
COPY pyproject.toml uv.lock README.md ./
RUN pip install --no-cache-dir "pydantic-settings>=2.0" "pyyaml>=6.0" "pynacl>=1.5"

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

# The entrypoint links /root/.m2 and /root/.gitlibs to /data/m2 and
# /data/gitlibs, so the deps `clojure` fetches persist across restarts.
COPY entrypoint.sh /usr/local/bin/bench-entrypoint
RUN chmod +x /usr/local/bin/bench-entrypoint
ENTRYPOINT ["/usr/local/bin/bench-entrypoint"]

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
