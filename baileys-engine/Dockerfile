FROM node:20-alpine

# Build-time proxy (Xray on Docker host)
ARG PROXY_HOST=172.17.0.1
ARG HTTP_PROXY=http://${PROXY_HOST}:10809
ARG HTTPS_PROXY=http://${PROXY_HOST}:10809
ARG NPM_REGISTRY=https://registry.npmmirror.com

# Install git (needed for Baileys 7.x github deps)
RUN apk add --no-cache git

# Configure npm with proxy
RUN npm config set registry ${NPM_REGISTRY}
RUN npm config set proxy ${HTTP_PROXY}
RUN npm config set https-proxy ${HTTPS_PROXY}

WORKDIR /app

# Install dependencies
COPY package.json .
RUN npm install --omit=dev && npm cache clean --force

# Copy source
COPY src/ ./src/

# Runtime env
ENV NODE_ENV=production
ENV PORT=3500
ENV HTTP_PROXY=http://172.17.0.1:10809
ENV AUTH_DIR=/app/data/auth

# Data volume
VOLUME ["/app/data"]

EXPOSE 3500

CMD ["node", "src/index.js"]
