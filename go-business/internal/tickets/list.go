package tickets

import (
	"context"
	"database/sql"
	"strings"
)

type ListFilter struct {
	ConversationID             string
	UserID, Keyword, Direction string
	Statuses                   []string
	Page, Size                 int
}

func (r *Repository) Page(ctx context.Context, filter ListFilter) ([]map[string]any, int, error) {
	where := " WHERE 1=1"
	var args []any
	if filter.UserID != "" {
		where += " AND user_id = ?"
		args = append(args, filter.UserID)
	}
	if filter.ConversationID != "" {
		where += " AND id IN (SELECT ticket_id FROM conversation_tickets WHERE conversation_id = ?)"
		args = append(args, filter.ConversationID)
	}
	if len(filter.Statuses) > 0 {
		where += " AND status IN (?" + strings.Repeat(", ?", len(filter.Statuses)-1) + ")"
		for _, status := range filter.Statuses {
			args = append(args, status)
		}
	}
	if filter.Keyword != "" {
		where += " AND (LOWER(id) LIKE LOWER(?) ESCAPE '/' OR LOWER(description) LIKE LOWER(?) ESCAPE '/' OR LOWER(user_name) LIKE LOWER(?) ESCAPE '/')"
		keyword := "%" + strings.NewReplacer("/", "//", "%", "/%", "_", "/_").Replace(filter.Keyword) + "%"
		args = append(args, keyword, keyword, keyword)
	}
	result := []map[string]any{}
	tx, err := r.db.BeginTx(ctx, &sql.TxOptions{ReadOnly: true})
	if err != nil {
		return nil, 0, err
	}
	defer tx.Rollback()

	var total int
	if err := tx.QueryRowContext(ctx, "SELECT COUNT(*) FROM tickets"+where, args...).Scan(&total); err != nil {
		return nil, 0, err
	}
	if total == 0 || filter.Page-1 > (total-1)/filter.Size {
		return result, total, nil
	}
	direction := "DESC"
	if filter.Direction == "asc" {
		direction = "ASC"
	}
	rows, err := tx.QueryContext(ctx, ticketSelect+where+" ORDER BY created_at "+direction+", id ASC LIMIT ? OFFSET ?",
		append(args, filter.Size, (filter.Page-1)*filter.Size)...)
	if err != nil {
		return nil, 0, err
	}
	defer rows.Close()
	for rows.Next() {
		ticket, err := scanTicket(rows)
		if err != nil {
			return nil, 0, err
		}
		result = append(result, ticket.payload())
	}
	return result, total, rows.Err()
}
