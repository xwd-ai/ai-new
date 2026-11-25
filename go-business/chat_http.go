package main

import (
	"bufio"
	"bytes"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net"
	"net/http"
	"net/url"
	"os"
	"strconv"
	"strings"
	"time"
)

type publicChat struct {
	store    chatStore
	agentURL *url.URL
	token    string
	client   *http.Client
}

func newPublicChat(store chatStore) publicChat {
	raw := strings.TrimSpace(os.Getenv("AGENT_INTERNAL_URL"))
	base, err := url.Parse(raw)
	if err != nil || base.Host == "" || (base.Scheme != "http" && base.Scheme != "https") {
		base = nil
	}
	return publicChat{
		store: store, agentURL: base, token: strings.TrimSpace(os.Getenv("AGENT_INTERNAL_TOKEN")),
		client: &http.Client{
			Timeout: 5 * time.Minute,
			Transport: &http.Transport{
				DialContext:     (&net.Dialer{Timeout: 5 * time.Second, KeepAlive: 30 * time.Second}).DialContext,
				IdleConnTimeout: 90 * time.Second,
			},
			CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse },
		},
	}
}

func (c publicChat) userID(r *http.Request) (string, error) {
	user, err := resolveTicketUser(c.store.read, r)
	if errors.Is(err, sql.ErrNoRows) {
		return "", nil
	}
	return user.ID, err
}

func (c publicChat) handleConversation(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Cache-Control", "no-store")
	query := r.URL.Query()
	offset, limit := 0, 100
	paged := query.Has("offset") || query.Has("limit")
	var err error
	if query.Has("offset") {
		offset, err = strconv.Atoi(query.Get("offset"))
		if err != nil || offset < 0 {
			writeJSON(w, http.StatusUnprocessableEntity, map[string]string{"detail": "offset 必须是非负整数"})
			return
		}
	}
	if query.Has("limit") {
		limit, err = strconv.Atoi(query.Get("limit"))
		if err != nil || limit < 1 || limit > 100 {
			writeJSON(w, http.StatusUnprocessableEntity, map[string]string{"detail": "limit 必须为 1 到 100"})
			return
		}
	}
	userID, err := c.userID(r)
	if err == nil {
		var result map[string]any
		result, err = c.store.conversation(r.Context(), userID, offset, limit, paged)
		if err == nil {
			writeJSON(w, http.StatusOK, result)
			return
		}
	}
	chatFailure(w, http.StatusInternalServerError, "读取会话失败", err)
}

type agentReply struct {
	Reply     *string         `json:"reply"`
	Suggest   bool            `json:"suggest_ticket"`
	Sources   json.RawMessage `json:"sources"`
	Trace     json.RawMessage `json:"trace"`
	TriedStep string          `json:"tried_step"`
}

func (c publicChat) respond(w http.ResponseWriter, r *http.Request, stream bool) {
	fields, ok := decodePublicObject(w, r)
	if !ok {
		writeJSON(w, http.StatusUnprocessableEntity, map[string]string{"detail": "请求体必须是 JSON 对象"})
		return
	}
	message, valid := ticketString(fields["message"])
	if !valid {
		writeJSON(w, http.StatusUnprocessableEntity, map[string]string{"detail": "message 必须是字符串"})
		return
	}
	if c.agentURL == nil || c.token == "" {
		chatFailure(w, http.StatusServiceUnavailable, "Agent 服务未配置", nil)
		return
	}
	userID, err := c.userID(r)
	if err != nil {
		chatFailure(w, http.StatusInternalServerError, "读取登录状态失败", err)
		return
	}
	if userID == "" {
		userID = "anonymous"
	}
	id, history, err := c.store.prepare(r.Context(), userID)
	if err != nil {
		chatFailure(w, http.StatusInternalServerError, "读取会话失败", err)
		return
	}
	payload, err := json.Marshal(map[string]any{"user_id": userID, "message": message, "history": history})
	if err != nil {
		chatFailure(w, http.StatusInternalServerError, "编码 Agent 请求失败", err)
		return
	}
	action := "reply"
	if stream {
		action = "stream"
	}
	target := c.agentURL.JoinPath("internal", "agent", action)
	request, err := http.NewRequestWithContext(r.Context(), http.MethodPost, target.String(), bytes.NewReader(payload))
	if err != nil {
		chatFailure(w, http.StatusBadGateway, "Agent 请求失败", err)
		return
	}
	request.Header.Set("X-Agent-Token", c.token)
	request.Header.Set("Content-Type", "application/json")
	response, err := c.client.Do(request)
	if err != nil {
		chatFailure(w, http.StatusBadGateway, "Agent 服务暂不可用", err)
		return
	}
	defer response.Body.Close()
	if response.StatusCode == http.StatusTooManyRequests {
		var detail map[string]any
		if json.NewDecoder(io.LimitReader(response.Body, 1<<16)).Decode(&detail) != nil {
			detail = map[string]any{"detail": "请求过于频繁，请稍后再试"}
		}
		w.Header().Set("Retry-After", response.Header.Get("Retry-After"))
		writeJSON(w, response.StatusCode, detail)
		return
	}
	if response.StatusCode != http.StatusOK {
		chatFailure(w, http.StatusBadGateway, "Agent 服务暂不可用", fmt.Errorf("Agent HTTP %d", response.StatusCode))
		return
	}
	if stream {
		if !strings.HasPrefix(response.Header.Get("Content-Type"), "text/event-stream") {
			chatFailure(w, http.StatusBadGateway, "Agent 流式响应无效", nil)
			return
		}
		c.forwardChatStream(w, r, response.Body, userID, id, message)
		return
	}
	var result agentReply
	if err := json.NewDecoder(io.LimitReader(response.Body, 8<<20)).Decode(&result); err != nil || result.Reply == nil {
		chatFailure(w, http.StatusBadGateway, "Agent 响应无效", err)
		return
	}
	if err := c.store.saveTurn(r.Context(), userID, id, message, *result.Reply, result.TriedStep); err != nil {
		chatFailure(w, http.StatusInternalServerError, "保存对话失败", err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"reply": *result.Reply, "suggest_ticket": result.Suggest, "sources": result.Sources, "trace": result.Trace,
	})
}

func (c publicChat) forwardChatStream(w http.ResponseWriter, r *http.Request, body io.Reader, userID, id, message string) {
	w.Header().Set("Content-Type", "text/event-stream")
	w.Header().Set("Cache-Control", "no-cache")
	w.Header().Set("X-Accel-Buffering", "no")
	controller := http.NewResponseController(w)
	scanner := bufio.NewScanner(body)
	scanner.Buffer(make([]byte, 4096), 8<<20)
	var event, data string
frames:
	for scanner.Scan() {
		line := scanner.Text()
		switch {
		case strings.HasPrefix(line, "event:"):
			event = strings.TrimSpace(strings.TrimPrefix(line, "event:"))
		case strings.HasPrefix(line, "data:"):
			data = strings.TrimSpace(strings.TrimPrefix(line, "data:"))
		case line == "" && event != "":
			if event == "done" {
				var result agentReply
				if json.Unmarshal([]byte(data), &result) != nil || result.Reply == nil {
					break frames
				}
				if err := c.store.saveTurn(r.Context(), userID, id, message, *result.Reply, ""); err != nil {
					slog.Error("保存流式对话失败", "err", err)
					_, _ = fmt.Fprint(w, "event: error\ndata: {\"message\":\"保存对话失败\"}\n\n")
					_ = controller.Flush()
					return
				}
			}
			if _, err := fmt.Fprintf(w, "event: %s\ndata: %s\n\n", event, data); err != nil {
				return
			}
			if err := controller.Flush(); err != nil || event == "done" || event == "error" {
				return
			}
			event, data = "", ""
		}
	}
	if r.Context().Err() == nil {
		_, _ = fmt.Fprint(w, "event: error\ndata: {\"message\":\"生成中断\"}\n\n")
		_ = controller.Flush()
	}
}

func chatFailure(w http.ResponseWriter, status int, message string, err error) {
	if err != nil {
		slog.Error(message, "err", err)
	}
	writeJSON(w, status, map[string]string{"detail": message})
}
