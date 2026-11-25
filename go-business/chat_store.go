package main

import (
	"aibusiness/internal/db"
	"context"
	"crypto/rand"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"errors"
	"math"
	"slices"
	"time"
)

type chatStore struct {
	read, write *sql.DB
}

type chatMessage struct {
	Role    string `json:"role"`
	Content string `json:"content"`
}

type storedMessage struct {
	chatMessage
	At float64 `json:"at"`
}

const latestConversation = `
	SELECT id FROM conversations WHERE user_id = ?
	ORDER BY updated_at DESC, created_at DESC, id DESC LIMIT 1`

func chatNow() float64 { return float64(time.Now().UnixNano()) / 1e9 }

func chatRecordID() string {
	var id [16]byte
	rand.Read(id[:])
	return hex.EncodeToString(id[:])
}

func (s chatStore) prepare(ctx context.Context, userID string) (string, []chatMessage, error) {
	var id string
	err := s.read.QueryRowContext(ctx, latestConversation, userID).Scan(&id)
	if errors.Is(err, sql.ErrNoRows) {
		id, err = s.createConversation(ctx, userID)
	}
	if err != nil {
		return "", nil, err
	}

	rows, err := s.read.QueryContext(ctx, `
		SELECT role, content FROM messages WHERE conversation_id = ?
		ORDER BY at DESC, id DESC LIMIT 8`, id)
	if err != nil {
		return "", nil, err
	}
	defer rows.Close()
	messages := make([]chatMessage, 0, 8)
	for rows.Next() {
		var message chatMessage
		if err := rows.Scan(&message.Role, &message.Content); err != nil {
			return "", nil, err
		}
		messages = append(messages, message)
	}
	slices.Reverse(messages)
	return id, messages, rows.Err()
}

func (s chatStore) createConversation(ctx context.Context, userID string) (string, error) {
	tx, err := db.BeginWrite(ctx, s.write)
	if err != nil {
		return "", err
	}
	defer tx.Rollback()
	if db.IsMySQL(s.write) {
		if _, err := tx.ExecContext(ctx, `INSERT INTO conversation_creation_locks (user_id) VALUES (?)
			ON DUPLICATE KEY UPDATE user_id = conversation_creation_locks.user_id`, userID); err != nil {
			return "", err
		}
	}
	var id string
	err = tx.QueryRowContext(ctx, latestConversation, userID).Scan(&id)
	if !errors.Is(err, sql.ErrNoRows) {
		return id, err
	}
	id, now := chatRecordID(), chatNow()
	if _, err := tx.ExecContext(ctx, `
		INSERT INTO conversations (id, user_id, title, created_at, updated_at)
		VALUES (?, ?, '', ?, ?)`, id, userID, now, now); err != nil {
		return "", err
	}
	return id, tx.Commit()
}

func (s chatStore) saveTurn(ctx context.Context, userID, id, message, reply, step string) error {
	tx, err := db.BeginWrite(ctx, s.write)
	if err != nil {
		return err
	}
	defer tx.Rollback()
	var previous float64
	if err := tx.QueryRowContext(ctx, db.LockRows(s.write, `
		SELECT updated_at FROM conversations WHERE id = ? AND user_id = ?`), id, userID).Scan(&previous); err != nil {
		return err
	}
	userAt := max(chatNow(), math.Nextafter(previous, math.Inf(1)))
	replyAt := math.Nextafter(userAt, math.Inf(1))
	if _, err := tx.ExecContext(ctx, `
		INSERT INTO messages (id, conversation_id, role, content, at)
		VALUES (?, ?, 'user', ?, ?), (?, ?, 'assistant', ?, ?)`,
		chatRecordID(), id, message, userAt, chatRecordID(), id, reply, replyAt); err != nil {
		return err
	}
	if _, err := tx.ExecContext(ctx, "UPDATE conversations SET updated_at = ? WHERE id = ?", replyAt, id); err != nil {
		return err
	}
	if step != "" {
		if _, err := tx.ExecContext(ctx, `
			INSERT INTO tried_steps (id, user_id, step, at) VALUES (?, ?, ?, ?)`,
			chatRecordID(), userID, step, replyAt); err != nil {
			return err
		}
	}
	return tx.Commit()
}

func (s chatStore) conversation(ctx context.Context, userID string, offset, limit int, paged bool) (map[string]any, error) {
	messages := []storedMessage{}
	response := map[string]any{"messages": messages}
	if paged {
		response["total"] = 0
	}
	if userID == "" {
		return response, nil
	}
	tx, err := s.read.BeginTx(ctx, &sql.TxOptions{ReadOnly: true})
	if err != nil {
		return nil, err
	}
	defer tx.Rollback()
	var id string
	err = tx.QueryRowContext(ctx, latestConversation, userID).Scan(&id)
	if errors.Is(err, sql.ErrNoRows) {
		return response, nil
	}
	if err != nil {
		return nil, err
	}
	rows, err := tx.QueryContext(ctx, `
		SELECT id, role, content, at FROM messages WHERE conversation_id = ?
		ORDER BY at ASC, id ASC`, id)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	digest := sha256.New()
	encoder := json.NewEncoder(digest)
	total := 0
	for rows.Next() {
		var recordID string
		var message storedMessage
		if err := rows.Scan(&recordID, &message.Role, &message.Content, &message.At); err != nil {
			return nil, err
		}
		if paged {
			if err := encoder.Encode([]any{message.Role, message.Content, message.At, recordID}); err != nil {
				return nil, err
			}
		}
		if !paged || (total >= offset && total-offset < limit) {
			messages = append(messages, message)
		}
		total++
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}
	response["messages"] = messages
	if paged {
		response["total"] = total
		response["conversation_id"] = id
		response["history_revision"] = hex.EncodeToString(digest.Sum(nil))
	}
	return response, nil
}
