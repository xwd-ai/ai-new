package main

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"log/slog"
	"net/http"
	"strconv"
	"strings"
	"unicode/utf8"
)

func publicSource(conn *sql.DB) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		var id, title, kind, content, rawKeywords string
		err := conn.QueryRowContext(r.Context(), `
			SELECT id, title, kind, content, keywords FROM knowledge_docs WHERE id = ?`,
			r.PathValue("source_id")).Scan(&id, &title, &kind, &content, &rawKeywords)
		if errors.Is(err, sql.ErrNoRows) {
			writeJSON(w, http.StatusNotFound, map[string]string{"detail": "来源不存在"})
			return
		}
		var keywords []string
		if err == nil {
			err = json.Unmarshal([]byte(rawKeywords), &keywords)
		}
		if err != nil {
			knowledgeFailure(w, err)
			return
		}
		if keywords == nil {
			keywords = []string{}
		}
		writeJSON(w, http.StatusOK, map[string]any{
			"id": id, "title": title, "kind": kind, "content": content, "keywords": keywords,
		})
	}
}

func knowledgeAdmin(w http.ResponseWriter, r *http.Request, conn *sql.DB) bool {
	user, err := resolveTicketUser(conn, r)
	if err != nil && !errors.Is(err, sql.ErrNoRows) {
		knowledgeFailure(w, err)
		return false
	}
	if !user.IsAdmin {
		writeJSON(w, http.StatusForbidden, map[string]string{"detail": "需要管理员权限"})
		return false
	}
	return true
}

func publicKnowledgeItems(conn *sql.DB) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if !knowledgeAdmin(w, r, conn) {
			return
		}
		offset, limit, valid := knowledgePage(r, -1)
		q := r.URL.Query()
		title, kind := q.Get("title"), q.Get("kind")
		if !valid || utf8.RuneCountInString(title) > 200 ||
			(q.Has("kind") && kind != "manual" && kind != "faq" && kind != "rule") {
			writeJSON(w, http.StatusUnprocessableEntity, map[string]string{"detail": "知识条目筛选参数不合法"})
			return
		}
		filter := " FROM knowledge_docs WHERE 1=1"
		args := []any{}
		if title = strings.TrimSpace(title); title != "" {
			// LIKE 与 SQLite 上原接口一致，显式转义字面通配符。
			filter += " AND title LIKE '%' || ? || '%' ESCAPE '/'"
			args = append(args, strings.NewReplacer("/", "//", "%", "/%", "_", "/_").Replace(title))
		}
		if kind != "" {
			filter += " AND kind = ?"
			args = append(args, kind)
		}
		items, total, err := queryKnowledgePage(r.Context(), conn,
			"id, title, kind", filter, "id", args, offset, limit, true,
			func(rows *sql.Rows) (map[string]any, error) {
				var id, title, kind string
				err := rows.Scan(&id, &title, &kind)
				return map[string]any{"id": id, "title": title, "kind": kind}, err
			})
		if err != nil {
			knowledgeFailure(w, err)
			return
		}
		writeJSON(w, http.StatusOK, map[string]any{"items": items, "total": total})
	}
}

func publicPendingIngests(conn *sql.DB) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if !knowledgeAdmin(w, r, conn) {
			return
		}
		offset, limit, valid := knowledgePage(r, 100)
		q := r.URL.Query()
		stage := q.Get("stage")
		if !valid || (q.Has("stage") && stage != "parsed" && stage != "chunked") {
			writeJSON(w, http.StatusUnprocessableEntity, map[string]string{"detail": "待恢复入库筛选参数不合法"})
			return
		}
		filter := " FROM knowledge_ingest_journal WHERE stage <> 'stored'"
		args := []any{}
		if stage != "" {
			filter += " AND stage = ?"
			args = append(args, stage)
		}
		paged := q.Has("offset") || q.Has("limit") || q.Has("stage")
		items, total, err := queryKnowledgePage(r.Context(), conn,
			"fingerprint, knowledge_id, stage, chunk_count, updated_at", filter,
			"updated_at, fingerprint", args, offset, limit, paged,
			func(rows *sql.Rows) (map[string]any, error) {
				var fingerprint, id, stage string
				var count int
				var updated float64
				err := rows.Scan(&fingerprint, &id, &stage, &count, &updated)
				return map[string]any{"fingerprint": fingerprint, "knowledge_id": id, "stage": stage,
					"chunk_count": count, "updated_at": updated}, err
			})
		if err != nil {
			knowledgeFailure(w, err)
			return
		}
		response := map[string]any{"items": items}
		if paged {
			response["total"] = total
		}
		writeJSON(w, http.StatusOK, response)
	}
}

func knowledgePage(r *http.Request, defaultLimit int) (offset, limit int, valid bool) {
	q := r.URL.Query()
	var err error
	limit = defaultLimit
	if q.Has("offset") {
		offset, err = strconv.Atoi(q.Get("offset"))
		if err != nil || offset < 0 {
			return 0, 0, false
		}
	}
	if q.Has("limit") {
		limit, err = strconv.Atoi(q.Get("limit"))
		if err != nil || limit < 1 || limit > 100 {
			return 0, 0, false
		}
	}
	return offset, limit, true
}

func queryKnowledgePage(ctx context.Context, conn *sql.DB, columns, filter, order string, args []any,
	offset, limit int, count bool, scan func(*sql.Rows) (map[string]any, error),
) ([]map[string]any, int, error) {
	tx, err := conn.BeginTx(ctx, &sql.TxOptions{ReadOnly: true})
	if err != nil {
		return nil, 0, err
	}
	defer tx.Rollback()
	items := []map[string]any{}
	var total int
	if count {
		if err := tx.QueryRowContext(ctx, "SELECT COUNT(*)"+filter, args...).Scan(&total); err != nil {
			return nil, 0, err
		}
		if offset >= total {
			return items, total, nil
		}
	}
	rows, err := tx.QueryContext(ctx, "SELECT "+columns+filter+" ORDER BY "+order+" LIMIT ? OFFSET ?", append(args, limit, offset)...)
	if err != nil {
		return nil, 0, err
	}
	defer rows.Close()
	for rows.Next() {
		item, err := scan(rows)
		if err != nil {
			return nil, 0, err
		}
		items = append(items, item)
	}
	return items, total, rows.Err()
}

func knowledgeFailure(w http.ResponseWriter, err error) {
	slog.Error("读取知识资料失败", "err", err)
	writeJSON(w, http.StatusInternalServerError, map[string]string{"detail": "读取知识资料失败"})
}
