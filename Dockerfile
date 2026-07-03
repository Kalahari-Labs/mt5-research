# market-intel executor — engine + dashboard container.
#
# The MT5 terminal itself CANNOT run in this container (it is a Windows GUI
# app). Run the bridge next to your terminal (Windows native, or Wine on the
# Docker host) and point this container at it:
#
#   docker compose up -d        # see docker-compose.yml
#
# The container is read-only toward your machine and holds no credentials:
# your broker login lives only in the MT5 terminal outside the container.
FROM python:3.12-slim

RUN pip install --no-cache-dir numpy && useradd -m executor
WORKDIR /app
COPY intel/executor /app/executor
RUN mkdir -p /app/logs /app/executor/data && chown -R executor /app
USER executor

# bridge is external by definition inside Docker
ENV MI_BRIDGE_SPAWN=0 \
    MI_BRIDGE_HOST=host.docker.internal \
    MI_DASH_HOST=0.0.0.0

EXPOSE 8877
CMD ["python", "-m", "executor.run"]
