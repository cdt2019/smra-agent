# 浏览器容器（SMRA_BROWSER=docker）。宿主按自己装的 Playwright 版本构建，两边版本必须一致：
#   docker build -f browser/Dockerfile --build-arg PLAYWRIGHT_VERSION=1.65.2 -t smra-browser:1.65.2 browser
# 镜像里只有 Playwright 和 Chromium，没有任何秘密；登录状态存在宿主上，每次连上时交给浏览器。
FROM python:3.12-slim

ARG PLAYWRIGHT_VERSION
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    PYTHONDONTWRITEBYTECODE=1

# libatomic1：Playwright 自带的 node 要它；fonts-noto-cjk：中文网页不显示成方块
RUN apt-get update \
    && apt-get install -y --no-install-recommends libatomic1 fonts-noto-cjk \
    && pip install --no-cache-dir "playwright==${PLAYWRIGHT_VERSION}" \
    && playwright install --with-deps chromium ffmpeg \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 1000 --no-create-home pw

USER pw
# 容器以只读根文件系统启动，只有 /tmp 可写；浏览器的临时配置、录像都在那里，容器删掉就没了
ENV HOME=/tmp
EXPOSE 3000
