# syntax=docker/dockerfile:1

FROM node:22-alpine AS web-builder
WORKDIR /src/web
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY web/ ./
RUN npm run build

FROM golang:1.22-alpine AS go-builder
ARG TARGETARCH=amd64
ARG GOPROXY=https://proxy.golang.org,direct
WORKDIR /src
COPY go.mod go.sum ./
RUN GOPROXY="${GOPROXY}" go mod download
COPY . ./
COPY --from=web-builder /src/web/dist ./web/dist
RUN go test ./... && \
    CGO_ENABLED=0 GOOS=linux GOARCH="${TARGETARCH}" \
    go build -trimpath -ldflags='-s -w' -o /out/todo2api ./cmd/todo2api

FROM alpine:3.22
RUN apk add --no-cache ca-certificates tzdata && \
    addgroup -S -g 10001 todo2api && \
    adduser -S -D -H -u 10001 -G todo2api todo2api
WORKDIR /app
COPY --from=go-builder --chown=10001:10001 /out/todo2api /app/todo2api
USER 10001:10001
EXPOSE 8080
ENTRYPOINT ["/app/todo2api"]
CMD ["-config", "/config/config.yaml"]
