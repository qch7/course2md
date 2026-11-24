# syntax=docker/dockerfile:1
# course2md 镜像。构建上下文为仓库根目录：
#   docker build -f deploy/Dockerfile --target hub    -t course2md-hub .
#   docker build -f deploy/Dockerfile --target worker -t course2md-worker .
# EXTRA_PACKAGES 可声明要预装的来源解析插件，例如 --build-arg EXTRA_PACKAGES="course2md-example"

ARG PYTHON_VERSION=3.12

# ------------------------------------------------------------------ 公共基础
FROM python:${PYTHON_VERSION}-slim AS base
COPY --from=ghcr.io/astral-sh/uv:0.10 /uv /usr/local/bin/uv
ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH=/opt/venv/bin:$PATH
WORKDIR /app
RUN useradd --create-home --uid 10001 course2md \
    && mkdir -p /data && chown course2md /data

# ------------------------------------------------------------------ 前端（只在构建时需要 Node）
# 产物由 vite.config.ts 写到 ../src/course2md/web_dist，即本阶段的 /build/src/course2md/web_dist。
# 不依赖构建上下文里预先构建的 web_dist（.dockerignore 已排除），避免镜像带上过期的前端。
FROM node:22-slim AS web
WORKDIR /build/web
COPY web/package.json web/pnpm-lock.yaml web/pnpm-workspace.yaml ./
# pnpm 版本取自 package.json 的 packageManager，升级时只改那一处
RUN npm install -g "pnpm@$(node -p "require('./package.json').packageManager.split('@')[1]")"
RUN --mount=type=cache,target=/root/.local/share/pnpm/store \
    pnpm install --frozen-lockfile
COPY web ./
RUN pnpm build

# ------------------------------------------------------------------ Hub（不含转写依赖）
FROM base AS hub
ARG EXTRA_PACKAGES=""
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project --extra server
COPY src ./src
COPY --from=web /build/src/course2md/web_dist ./src/course2md/web_dist
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --extra server \
    && if [ -n "$EXTRA_PACKAGES" ]; then uv pip install $EXTRA_PACKAGES; fi
USER course2md
ENV COURSE2MD_OUT_DIR=/data
VOLUME /data
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/api/v1/auth/status', timeout=4)"
CMD ["course2md", "server", "--host", "0.0.0.0", "--port", "8765"]

# ------------------------------------------------------------------ 计算节点（转写 + CUDA 运行库）
FROM base AS worker
ARG EXTRA_PACKAGES=""
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project --extra asr --extra cuda
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --extra asr --extra cuda \
    && if [ -n "$EXTRA_PACKAGES" ]; then uv pip install $EXTRA_PACKAGES; fi
# Linux 上 CTranslate2 通过动态链接器查找 pip 安装的 cuBLAS / cuDNN
ENV LD_LIBRARY_PATH=/opt/venv/lib/python3.12/site-packages/nvidia/cublas/lib:/opt/venv/lib/python3.12/site-packages/nvidia/cudnn/lib \
    COURSE2MD_WORKER_DIR=/data/worker \
    HF_HOME=/data/hf-cache
USER course2md
VOLUME /data
CMD ["course2md", "worker", "--no-agents"]
