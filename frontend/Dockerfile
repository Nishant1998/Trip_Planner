# syntax=docker/dockerfile:1
FROM node:22-bookworm-slim AS build
WORKDIR /build
COPY package.json package-lock.json ./
RUN --mount=type=cache,target=/root/.npm npm ci
COPY . .
# Only public browser configuration belongs in build arguments.
ARG VITE_GOOGLE_CLIENT_ID
ENV VITE_GOOGLE_CLIENT_ID=$VITE_GOOGLE_CLIENT_ID VITE_API_BASE=""
RUN npm run build

FROM caddy:2-alpine
ENV SITE_ADDRESS=:80
COPY --from=build /build/dist /srv
COPY Caddyfile /etc/caddy/Caddyfile
HEALTHCHECK --interval=20s --timeout=5s --start-period=20s --retries=3 \
    CMD wget -q -O /dev/null http://127.0.0.1:8081/health
