# 构建上下文是仓库根目录：
#   docker build -f cell/Dockerfile -t smra-cell:dev .
FROM python:3.12-slim

RUN pip install --no-cache-dir anthropic \
    && useradd --create-home --uid 1000 agent

# 运行程序烤进镜像。容器以只读根文件系统启动，所以电脑里改不了它。
COPY cell/agent_run.py cell/cell_tools.py /opt/agent/
# 中继脚本只在中继容器里用。它不含任何秘密，放在同一个镜像里省一次构建。
COPY host/relay.py /opt/relay/relay.py

USER agent
WORKDIR /home/agent
CMD ["sleep", "infinity"]
