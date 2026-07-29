# syntax=docker/dockerfile:1

# Build the SPA inside the image so a production deployment never depends on
# node_modules from the server checkout.
FROM node:22-alpine AS web-build

WORKDIR /web

COPY web/package.json web/package-lock.json ./
ARG NPM_REGISTRY=https://mirrors.cloud.tencent.com/npm/
RUN npm ci --registry=${NPM_REGISTRY}

COPY web/ ./
RUN npm run build

# Caddy owns the public ports: it terminates HTTPS, serves the SPA bundle, and
# is the only service allowed to proxy requests to the API.
FROM caddy:2.10-alpine

COPY deploy/Caddyfile /etc/caddy/Caddyfile
COPY deploy/Caddyfile.staging /etc/caddy/Caddyfile.staging
COPY --from=web-build /web/dist /srv
