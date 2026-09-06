FROM node:22-bookworm-slim AS node
ARG CODEX_VERSION=0.153.3
RUN npm install --global @openai/codex@${CODEX_VERSION}

FROM python:3.12-slim-bookworm
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s /usr/local/lib/node_modules/@openai/codex/bin/codex.js /usr/local/bin/codex \
    && groupadd --gid 10001 radar && useradd --uid 10001 --gid radar --home-dir /data radar
WORKDIR /srv/radar
COPY server/requirements.lock ./requirements.lock
RUN pip install --no-cache-dir --require-hashes -r requirements.lock
COPY server/radar ./radar
COPY server/config.toml ./config.toml
RUN mkdir -p /data && chown radar:radar /data
USER radar
ENV HOME=/data CODEX_HOME=/data/codex RADAR_DATABASE_URL=sqlite:////data/radar.db
EXPOSE 18473
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:18473/healthz', timeout=2)"
CMD ["uvicorn", "radar.api:create_app", "--factory", "--host", "0.0.0.0", "--port", "18473", "--workers", "1", "--no-access-log"]
