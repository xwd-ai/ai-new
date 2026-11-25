// Package auth 使用与 Python 相同的 JWT 算法、密钥和声明约定。
package auth

import (
	"errors"
	"os"
	"slices"
	"strconv"
	"strings"
	"time"

	"github.com/golang-jwt/jwt/v5"
)

const (
	// Issuer / Audience 必须与 Python 侧一致，否则跨语言签发的令牌无法互认。
	Issuer   = "ai-new"
	Audience = "ai-new-web"

	// TokenTypeAccess 是 access token 的 typ 声明值；refresh token 不允许通过本校验。
	TokenTypeAccess  = "access"
	TokenTypeRefresh = "refresh"

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

func ClockLeeway() time.Duration {
	seconds, err := strconv.Atoi(strings.TrimSpace(os.Getenv("AI_NEW_JWT_LEEWAY")))
	if err != nil || seconds < 0 || seconds > 300 {
		return clockLeeway
	}
	return time.Duration(seconds) * time.Second
}

func HasConfiguredSecret() bool {
	return strings.TrimSpace(os.Getenv(secretEnv)) != ""
}

func secret() []byte {
	if value := strings.TrimSpace(os.Getenv(secretEnv)); value != "" {
		return []byte(value)
	}
	return []byte(devSecret)
}

// VerifyAccessToken 校验 access token，成功返回声明，失败返回具体原因。
//
// 校验项与 Python 对齐：算法固定 HS256、签名、iss、aud、exp（含 30 秒时钟偏差）、
// 以及 typ 必须为 access —— 与 Python 一样，refresh token 不能顶替 access token。
func VerifyAccessToken(raw string) (*Claims, error) {
	return verifyToken(raw, TokenTypeAccess, true)
}

func VerifyRefreshToken(raw string) (*Claims, error) {
	claims, err := verifyToken(raw, TokenTypeRefresh, true)
	if err != nil {
		return nil, err
	}
	if err := ValidateIdentityClaims(claims); err != nil {
		return nil, err
	}
	return claims, nil
}

func ValidateIdentityClaims(claims *Claims) error {
	if err := validateTokenFields(claims); err != nil {
		return err
	}
	if claims.IssuedAt.After(time.Now().Add(ClockLeeway())) {
		return jwt.ErrTokenUsedBeforeIssued
	}
	return nil
}

func validateTokenFields(claims *Claims) error {
	if claims.Subject == "" || claims.IssuedAt == nil || claims.ExpiresAt == nil ||
		!claims.ExpiresAt.After(claims.IssuedAt.Time) {
		return jwt.ErrTokenInvalidClaims
	}
	return nil
}

func verifyToken(raw, expectedType string, verifyTime bool) (*Claims, error) {
	claims := &Claims{}

	options := []jwt.ParserOption{
		// 只允许 HS256：防止 alg=none 或非对称算法降级攻击
		jwt.WithValidMethods([]string{"HS256"}),
		jwt.WithIssuer(Issuer),
		jwt.WithAudience(Audience),
		jwt.WithLeeway(ClockLeeway()),
		jwt.WithExpirationRequired(),
	}
	if !verifyTime {
		options = append(options, jwt.WithoutClaimsValidation())
	}

	token, err := jwt.NewParser(options...).ParseWithClaims(raw, claims, func(*jwt.Token) (any, error) {
		return secret(), nil
	})
	if err != nil {
		return nil, err
	}
	if !token.Valid {
		return nil, errors.New("令牌无效")
	}
	if claims.Typ != expectedType {
		return nil, ErrWrongType
	}
	if !verifyTime {
		if claims.Issuer != Issuer || !slices.Contains(claims.Audience, Audience) {
			return nil, jwt.ErrTokenInvalidClaims
		}
		if err := validateTokenFields(claims); err != nil {
			return nil, err
		}
	}
	return claims, nil
}
