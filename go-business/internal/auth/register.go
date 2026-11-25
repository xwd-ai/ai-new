package auth

import (
	"context"
	"crypto/rand"
	"database/sql"
	"encoding/hex"
	"errors"
	"regexp"
	"strings"
	"unicode"
	"unicode/utf8"

	"golang.org/x/crypto/bcrypt"
)

var (
	ErrInvalidUsername = errors.New("用户名需 3–32 位，以字母开头，仅可含字母、数字、下划线、点或连字符")
	ErrUsernameTaken   = errors.New("用户名已被占用")
	usernamePattern    = regexp.MustCompile(`^[A-Za-z][A-Za-z0-9_.-]{2,31}$`)
	// Unicode digits outside the decimal category, matching Python str.isdigit.
	additionalDigits = regexp.MustCompile(`[\x{b2}-\x{b3}\x{b9}\x{1369}-\x{1371}\x{19da}\x{2070}\x{2074}-\x{2079}\x{2080}-\x{2089}\x{2460}-\x{2468}\x{2474}-\x{247c}\x{2488}-\x{2490}\x{24ea}\x{24f5}-\x{24fd}\x{24ff}\x{2776}-\x{277e}\x{2780}-\x{2788}\x{278a}-\x{2792}\x{10a40}-\x{10a43}\x{10e60}-\x{10e68}\x{11052}-\x{1105a}\x{1f100}-\x{1f10a}]`)
	weakPasswords    = map[string]bool{
		"12345678": true, "123456789": true, "1234567890": true, "11111111": true, "88888888": true,
		"password": true, "password1": true, "passw0rd": true, "p@ssw0rd": true, "qwerty123": true,
		"abc12345": true, "a1234567": true, "admin123": true, "iloveyou": true, "letmein1": true,
	}
)

type WeakPasswordError struct{ Violations []string }

func (*WeakPasswordError) Error() string { return "密码强度不足" }

func Register(ctx context.Context, conn *sql.DB, username, password, name string) (Account, error) {
	username = strings.TrimSpace(username)
	if !usernamePattern.MatchString(username) {
		return Account{}, ErrInvalidUsername
	}
	if violations := passwordViolations(password, username); len(violations) > 0 {
		return Account{}, &WeakPasswordError{Violations: violations}
	}
	hash, err := bcrypt.GenerateFromPassword([]byte(password), bcryptCost())
	if err != nil {
		return Account{}, err
	}
	if name = strings.TrimSpace(name); name == "" {
		name = username
	}
	var id [5]byte
	rand.Read(id[:])
	user := Account{ID: "u-" + hex.EncodeToString(id[:]), Username: username, Name: name, Role: "customer"}
	tx, err := conn.BeginTx(ctx, nil)
	if err != nil {
		return Account{}, err
	}
	defer tx.Rollback()
	var exists bool
	if err := tx.QueryRowContext(ctx, "SELECT EXISTS (SELECT 1 FROM users WHERE lower(username) = lower(?))", username).Scan(&exists); err != nil {
		return Account{}, err
	}
	if exists {
		return Account{}, ErrUsernameTaken
	}
	if _, err := tx.ExecContext(ctx, `
		INSERT INTO users (id, username, name, role, password_hash) VALUES (?, ?, ?, ?, ?)`,
		user.ID, user.Username, user.Name, user.Role, string(hash)); err != nil {
		return Account{}, err
	}
	if err := tx.Commit(); err != nil {
		return Account{}, err
	}
	return user, nil
}

func passwordViolations(password, username string) []string {
	if password == "" {
		return []string{"密码不能为空"}
	}
	var violations []string
	if utf8.RuneCountInString(password) < 8 {
		violations = append(violations, "长度至少 8 位")
	}
	if len(password) > 72 {
		violations = append(violations, "长度不能超过 72 字节")
	}
	var space, letter, digit, different bool
	first, _ := utf8.DecodeRuneInString(password)
	for _, ch := range password {
		space = space || unicode.IsSpace(ch) || (ch >= 0x1c && ch <= 0x1f)
		letter = letter || unicode.IsLetter(ch)
		digit = digit || unicode.IsDigit(ch) || additionalDigits.MatchString(string(ch))
		different = different || ch != first
	}
	if space {
		violations = append(violations, "不能包含空格等空白字符")
	}
	if !letter || !digit {
		violations = append(violations, "需同时包含字母与数字")
	}
	if !different {
		violations = append(violations, "不能是同一个字符的重复")
	}
	lower := strings.ToLower(password)
	if weakPasswords[lower] {
		violations = append(violations, "不能使用常见弱口令")
	}
	if strings.Contains(lower, strings.ToLower(username)) {
		violations = append(violations, "不能包含用户名")
	}
	return violations
}
