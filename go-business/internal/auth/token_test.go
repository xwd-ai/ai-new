package auth

import (
	"testing"
	"time"

	"github.com/golang-jwt/jwt/v5"
)

// signToken 用与 Python 相同的密钥与声明约定生成令牌，
// 用于验证「Go 能校验 Python 签发格式」。
func signToken(t *testing.T, typ string, expOffset time.Duration) string {
	t.Helper()

	now := time.Now()
	claims := Claims{
		Typ: typ,
		RegisteredClaims: jwt.RegisteredClaims{
			Issuer:    Issuer,
			Subject:   "u-test",
			Audience:  jwt.ClaimStrings{Audience},
			ID:        "jti-test",
			IssuedAt:  jwt.NewNumericDate(now),
			ExpiresAt: jwt.NewNumericDate(now.Add(expOffset)),
		},
	}

	raw, err := jwt.NewWithClaims(jwt.SigningMethodHS256, claims).SignedString(secret())
	if err != nil {
		t.Fatalf("签发令牌失败: %v", err)
	}
	return raw
}

func TestVerifyAccessToken(t *testing.T) {
	tests := []struct {
		name    string
		token   func(t *testing.T) string
		wantErr bool
	}{
		{
			name:  "合法 access token",
			token: func(t *testing.T) string { return signToken(t, "access", time.Hour) },
		},
		{
			name:    "refresh token 不能顶替",
			token:   func(t *testing.T) string { return signToken(t, "refresh", time.Hour) },
			wantErr: true,
		},
		{
			name:    "已过期",
			token:   func(t *testing.T) string { return signToken(t, "access", -time.Hour) },
			wantErr: true,
		},
		{
			name:    "非法格式",
			token:   func(t *testing.T) string { return "not-a-jwt" },
			wantErr: true,
		},
		{
			name: "签名被篡改",
			token: func(t *testing.T) string {
				raw := signToken(t, "access", time.Hour)
				return raw[:len(raw)-4] + "abcd"
			},
			wantErr: true,
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			claims, err := VerifyAccessToken(tt.token(t))
			if tt.wantErr {
				if err == nil {
					t.Fatalf("期望校验失败，实际通过，claims=%+v", claims)
				}
				return
			}
			if err != nil {
				t.Fatalf("期望校验通过，实际失败: %v", err)
			}
			if claims.Subject != "u-test" {
				t.Errorf("sub 期望 u-test，实际 %s", claims.Subject)
			}
			if claims.Typ != TokenTypeAccess {
				t.Errorf("typ 期望 %s，实际 %s", TokenTypeAccess, claims.Typ)
			}
		})
	}
}

// TestRejectAlgNone 验证不会接受 alg=none 的降级攻击。
func TestRejectAlgNone(t *testing.T) {
	claims := Claims{
		Typ: TokenTypeAccess,
		RegisteredClaims: jwt.RegisteredClaims{
			Issuer:    Issuer,
			Subject:   "u-test",
			Audience:  jwt.ClaimStrings{Audience},
			ExpiresAt: jwt.NewNumericDate(time.Now().Add(time.Hour)),
		},
	}

	raw, err := jwt.NewWithClaims(jwt.SigningMethodNone, claims).SignedString(jwt.UnsafeAllowNoneSignatureType)
	if err != nil {
		t.Fatalf("构造 alg=none 令牌失败: %v", err)
	}

	if _, err := VerifyAccessToken(raw); err == nil {
		t.Fatal("必须拒绝 alg=none 的令牌")
	}
}
