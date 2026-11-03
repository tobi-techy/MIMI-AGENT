# Mimi iMessage bridge (Spectrum Cloud). A small long-running Node process:
# no web port, it only makes outbound connections (Spectrum gRPC, Mimi API).
#   docker build -t mimi-imessage-bridge bridges/imessage
#   docker run --env-file bridges/imessage/.env \
#     -v "$PWD/bridges/imessage/bridge.config.json:/app/bridge.config.json:ro" \
#     -v mimi-bridge-state:/app/state mimi-imessage-bridge
FROM node:22-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --omit=dev --no-audit --no-fund
COPY src ./src
COPY data ./data
RUN mkdir -p /app/state && chown -R node:node /app/state
USER node
ENV NODE_ENV=production BRIDGE_STATE_DIR=/app/state BRIDGE_CONFIG=/app/bridge.config.json
VOLUME ["/app/state"]
CMD ["node", "src/main-cloud.ts"]
