# 多阶段构建：编译阶段带完整工具链，运行阶段只留静态二进制，
# 最终镜像约 15MB，不含 Go 工具链。
FROM golang:1.27-alpine AS builder

WORKDIR /build

# 先只拷贝依赖清单并下载：代码变动时这一层可复用缓存
COPY go.mod go.sum ./
RUN go mod download

COPY . .

# CGO_ENABLED=0 产出静态链接二进制，才能在 alpine 上直接运行
RUN CGO_ENABLED=0 GOOS=linux go build -trimpath -ldflags="-s -w" -o /retrieval .

FROM alpine:3.20

# 需要证书才能访问外部 HTTPS 服务
RUN apk add --no-cache ca-certificates

COPY --from=builder /retrieval /usr/local/bin/retrieval

# 容器内必须监听 0.0.0.0，否则宿主机无法访问
ENV ADDR=0.0.0.0:8081
EXPOSE 8081

# 非 root 用户运行
USER 65534:65534

ENTRYPOINT ["/usr/local/bin/retrieval"]
