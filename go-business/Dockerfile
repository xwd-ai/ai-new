# 多阶段构建：编译阶段带完整工具链，运行阶段只留静态二进制。
# modernc.org/sqlite 是纯 Go 驱动，CGO_ENABLED=0 也能编译。
FROM golang:1.27-alpine AS builder

WORKDIR /build

# 先只拷贝依赖清单并下载：代码变动时这一层可复用缓存
COPY go.mod go.sum ./
RUN go mod download

COPY . .

RUN CGO_ENABLED=0 GOOS=linux go build -trimpath -ldflags="-s -w" -o /business .

FROM alpine:3.20

RUN apk add --no-cache ca-certificates

COPY --from=builder /business /usr/local/bin/business

# 容器内必须监听 0.0.0.0；数据库文件由 compose 只读挂载到 /data/app.db
ENV ADDR=0.0.0.0:8082
ENV DB_PATH=/data/app.db
EXPOSE 8082

USER 65534:65534

ENTRYPOINT ["/usr/local/bin/business"]
