# syntax=docker/dockerfile:1.7
# Stage 1: build the single-file IIFE bundle. Stage 2: serve it from nginx.
# Node and all build tooling stay in stage 1; the final image is nginx + static files.
ARG NODE_BASE_IMAGE=node:24-alpine
ARG NGINX_BASE_IMAGE=nginx:1.30-alpine

FROM ${NODE_BASE_IMAGE} AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN --mount=type=cache,target=/root/.npm npm ci --no-audit --no-fund
COPY . .
# Build-time default gateway URL (merchants can still override per embed).
ARG VITE_GATEWAY_BASE_URL
ENV VITE_GATEWAY_BASE_URL=${VITE_GATEWAY_BASE_URL}
RUN npm run build

FROM ${NGINX_BASE_IMAGE} AS runtime
COPY docker/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist/commerce-chat.js /usr/share/nginx/html/widget/commerce-chat.js
EXPOSE 80
