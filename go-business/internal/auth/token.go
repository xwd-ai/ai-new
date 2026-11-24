// Package auth 实现 access token 的校验，与 Python 的
// backend/services/auth.py 使用同一套算法、密钥与声明约定。
//
// 迁移范围刻意限定为**校验**：签发与登录仍留在 Python。理由是登录是低频写操作，
// 迁移动机不足；而校验是每个请求都会走的热点，且是纯计算，最容易独立部署。
package auth

import (
	"errors"
	"os"
	"time"

	"github.com/golang-jwt/jwt/v5"
)

const (
	// Issuer / Audience 必须与 Python 侧一致，否则跨语言签发的令牌无法互认。
	Issuer   = "ai-new"
	Audience = "ai-new-web"

	// TokenTypeAccess 是 access token 的 typ 声明值；refresh token 不允许通过本校验。
	TokenTypeAccess = "access"

	secretEnv = "AI_NEW_JWT_SECRET"
	// 与 Python 的内置开发密钥保持一致：本地调试时两边可互认令牌
	devSecret   = "ai-new-dev-secret-change-me-before-deploy"
	clockLeeway = 30 * time.Second
)

// Claims 是校验通过后返回的声明集合。
//
// 内嵌 jwt.RegisteredClaims 以获得 iss/aud/exp/iat/nbf/sub/jti 与接口方法；
// Typ 是 Python 侧额外放进载荷的自定义声明（标准 JWT 的 typ 在头部而非载荷）。
type Claims struct {
	Typ string `json:"typ"`
	jwt.RegisteredClaims
}

// ErrWrongType 表示令牌结构合法但用途不是 access。
var ErrWrongType = errors.New("令牌用途不匹配")

func secret() []byte {
	if value := os.Getenv(secretEnv); value != "" {
		return []byte(value)
	}
	return []byte(devSecret)
}

// VerifyAccessToken 校验 access token，成功返回声明，失败返回具体原因。
//
// 校验项与 Python 对齐：算法固定 HS256、签名、iss、aud、exp（含 30 秒时钟偏差）、
// 以及 typ 必须为 access —— 与 Python 一样，refresh token 不能顶替 access token。
func VerifyAccessToken(raw string) (*Claims, error) {
	claims := &Claims{}

	parser := jwt.NewParser(
		// 只允许 HS256：防止 alg=none 或非对称算法降级攻击
		jwt.WithValidMethods([]string{"HS256"}),
		jwt.WithIssuer(Issuer),
		jwt.WithAudience(Audience),
		jwt.WithLeeway(clockLeeway),
		jwt.WithExpirationRequired(),
	)

	token, err := parser.ParseWithClaims(raw, claims, func(*jwt.Token) (any, error) {
		return secret(), nil
	})
	if err != nil {
		return nil, err
	}
	if !token.Valid {
		return nil, errors.New("令牌无效")
	}
	if claims.Typ != TokenTypeAccess {
		return nil, ErrWrongType
	}
	return claims, nil
}
