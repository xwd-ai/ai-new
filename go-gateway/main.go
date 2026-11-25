// 网关：把 Python 后端与两个 Go 服务收在同一个对外入口后面。
//
// 定位是「入口」而不是「业务」：它不改写业务语义，只做几件在入口处才高效的事
// —— 统一鉴权可观测、入口限流、SSE 流转发、超时与优雅关闭、pprof。
//
// 它刻意**不接管授权判定**：授权仍只有 Python 一处权威。网关注入的身份只用于
// 日志与限流维度，不写进转发给 Python 的请求头 —— 否则 Python 必须信任该头，
// 一旦 Python 被直接暴露，那个头就成了可伪造的身份来源。纵深防御的原则是
// 「多一层校验」，不是「多一个信任源」。
//
// 所有依赖都可缺省：BACKEND_URL 是唯一必需项；没配 GO_BUSINESS_URL 或
// RETRIEVAL_URL 时对应能力自动关闭，请求照常透传，不会因为某个旁路服务
// 没部署而挡死主流程。这与项目里「远程服务只是加速，不是正确性依赖」一致。
package main

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net"
	"net/http"
	"net/http/httputil"
	"net/http/pprof"
	"net/url"
	"os"
	"os/signal"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"

	"golang.org/x/time/rate"

	"aigateway/internal/httplog"
	"aigateway/internal/ratelimit"
	"aigateway/internal/singleflight"
)

const (
	defaultAddr     = "127.0.0.1:8080"
	shutdownTimeout = 10 * time.Second

	// 转发检索请求时的请求体上限。文档集合随请求体传入，放任超大请求
	// 会把网关内存打满；4 MiB 足够覆盖正常的知识库规模。
	maxStreamBodyBytes = 4 << 20

	// 探测上游健康状态与校验令牌的超时。两者都是入口路径上的旁路调用，
	// 必须短，不能让它们的抖动拖慢主请求。
	probeTimeout = 2 * time.Second
)

// config 是网关的全部可配置项，全部来自环境变量。
type config struct {
	addr             string
	backendURL       *url.URL // 必需：其余能力都挂在它前面
	retrievalURL     *url.URL // 可选：提供 /api/retrieval/stream
	businessURL      *url.URL // 可选：提供入口令牌校验
	upstreamTimeout  time.Duration
	rateLimit        float64 // 每秒补充的令牌数，<=0 表示关闭入口限流
	rateBurst        int
	pprof            bool
	goPendingTickets bool
	goTicketWrites   bool
	frontendDir      string
}

var (
	cfg    config
	logger *slog.Logger

	backendProxy  *httputil.ReverseProxy
	businessProxy *httputil.ReverseProxy
	businessHTTP  *http.Client
	authGroup     = singleflight.New()
	entryLimiter  *ratelimit.Limiter
	streamClient  *http.Client
	emptyResponse = []byte(`{"error":"后端服务暂不可用"}`)
)

func main() {
	logger = slog.New(slog.NewJSONHandler(os.Stdout, &slog.HandlerOptions{Level: slog.LevelInfo}))
	slog.SetDefault(logger)

	cfg = loadConfig()

	backendProxy = newBackendProxy(cfg.backendURL)
	if (cfg.goPendingTickets || cfg.goTicketWrites) && cfg.businessURL != nil {
		businessProxy = newBackendProxy(cfg.businessURL)
	}
	businessHTTP = &http.Client{Timeout: probeTimeout, Transport: newTransport(false)}
	streamClient = &http.Client{Transport: newTransport(false)}

	mux := http.NewServeMux()
	registerRoutes(mux)

	// 中间件自外向内：访问日志 → CORS → 入口鉴权 → 入口限流 → 业务。
	// 鉴权必须排在限流之外：限流按键区分桶，需要先拿到鉴权注入的用户身份。
	handler := http.Handler(mux)
	if cfg.rateLimit > 0 {
		entryLimiter = ratelimit.New(ratelimit.Config{
			Limit:   rate.Limit(cfg.rateLimit),
			Burst:   cfg.rateBurst,
			IdleTTL: 10 * time.Minute,
		})
		handler = ratelimit.Middleware(entryLimiter, ratelimitKey)(handler)
	}
	handler = withAuth(handler)
	handler = withCORS(handler)
	handler = httplog.Middleware(logger)(handler)

	srv := &http.Server{
		Addr:              cfg.addr,
		Handler:           handler,
		ReadHeaderTimeout: 5 * time.Second,
		IdleTimeout:       120 * time.Second,
	}

	go func() {
		stop := make(chan os.Signal, 1)
		signal.Notify(stop, syscall.SIGINT, syscall.SIGTERM)
		<-stop

		logger.Info("收到退出信号，开始优雅关闭")
		ctx, cancel := context.WithTimeout(context.Background(), shutdownTimeout)
		defer cancel()
		// Shutdown 先停止接受新连接，再等在途请求结束；
		// 限流器与后台清理协程在请求都结束后再收，避免关到一半还在计时。
		if err := srv.Shutdown(ctx); err != nil {
			logger.Error("关闭异常", "err", err)
		}
		if entryLimiter != nil {
			entryLimiter.Close()
		}
	}()

	logger.Info("网关启动",
		"addr", cfg.addr,
		"backend", cfg.backendURL.String(),
		"retrieval", urlOrEmpty(cfg.retrievalURL),
		"business", urlOrEmpty(cfg.businessURL),
		"rate_limit", cfg.rateLimit,
		"pprof", cfg.pprof,
	)
	if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
		logger.Error("启动失败", "err", err)
		os.Exit(1)
	}
	logger.Info("网关已退出")
}

// registerRoutes 注册网关自有端点，其余请求全部交给反向代理。
//
// Go 1.22 起的 ServeMux 按「最具体模式优先」匹配，因此这几个精确模式
// 会先于兜底的 "/" 命中，不需要额外的前缀判断。
func registerRoutes(mux *http.ServeMux) {
	mux.HandleFunc("GET /health", handleHealth)
	mux.HandleFunc("POST /api/retrieval/stream", handleRetrievalStream)

	if cfg.pprof {
		mux.HandleFunc("GET /debug/pprof/", pprof.Index)
		mux.HandleFunc("GET /debug/pprof/cmdline", pprof.Cmdline)
		mux.HandleFunc("GET /debug/pprof/profile", pprof.Profile)
		mux.HandleFunc("GET /debug/pprof/symbol", pprof.Symbol)
		mux.HandleFunc("GET /debug/pprof/trace", pprof.Trace)
	}

	if businessProxy != nil {
		if cfg.goPendingTickets {
			mux.HandleFunc("GET /api/tickets/pending", proxyPublicTicket)
		}
		if cfg.goTicketWrites {
			for _, operation := range []string{"confirm", "cancel", "status"} {
				mux.HandleFunc("POST /api/tickets/"+operation, proxyPublicTicket)
			}
		}
	}

	registerFrontendRoutes(mux, cfg.frontendDir)
	mux.Handle("/", backendProxy)
}

func proxyPublicTicket(w http.ResponseWriter, r *http.Request) {
	upstream := r.Clone(r.Context())
	upstream.URL.Path = "/public" + r.URL.Path
	upstream.URL.RawPath = ""
	businessProxy.ServeHTTP(w, upstream)
}

// handleHealth 聚合三类上游的健康状态，并透传前端依赖的字段。
//
// 前端用 /health 的 stream 字段判断是否走流式接口，这里必须原样透传
// Python 的取值而不是硬编码 true —— 能力应由提供方决定，网关只做搬运。
// 上游不可达时返回 degraded 而不是失败：网关自身是健康的，
// 上游挂了要能从这一个响应里看出来，而不是只得到一个连不上的错误。
func handleHealth(w http.ResponseWriter, r *http.Request) {
	ctx, cancel := context.WithTimeout(r.Context(), probeTimeout)
	defer cancel()

	out := map[string]any{
		"status":  "ok",
		"gateway": true,
		"capabilities": map[string]bool{
			"auth_verify":      cfg.businessURL != nil,
			"rate_limit":       cfg.rateLimit > 0,
			"retrieval_stream": cfg.retrievalURL != nil,
			"pprof":            cfg.pprof,
		},
	}

	backend, err := fetchJSON(ctx, cfg.backendURL.String()+"/health")
	if err != nil {
		out["status"] = "degraded"
		out["backend_error"] = err.Error()
	} else {
		// 只透传前端实际消费的字段，其余包装在 backend 下，避免键名冲突
		if v, ok := backend["stream"]; ok {
			out["stream"] = v
		}
		if v, ok := backend["models"]; ok {
			out["models"] = v
		}
		out["backend"] = backend
	}

	// 两个 Go 服务并行探测：健康检查本身不该串行等两个超时叠加
	services := map[string]string{}
	var mu sync.Mutex
	var wg sync.WaitGroup
	probes := map[string]*url.URL{"retrieval": cfg.retrievalURL, "business": cfg.businessURL}
	for name, base := range probes {
		if base == nil {
			continue
		}
		wg.Add(1)
		go func(name string, base *url.URL) {
			defer wg.Done()
			status := "ok"
			if _, err := fetchJSON(ctx, base.String()+"/health"); err != nil {
				status = "down: " + err.Error()
			}
			mu.Lock()
			services[name] = status
			mu.Unlock()
		}(name, base)
	}
	wg.Wait()
	if len(services) > 0 {
		out["services"] = services
	}

	writeJSON(w, http.StatusOK, out)
}

// handleRetrievalStream 把 Go 检索服务的 /stream 转给客户端。
//
// 这是原先的「断头路」：检索服务早就实现了分阶段推送，但 Python 侧的 SSE
// 是另一套独立实现，那条流从没被消费过。网关在这里做单向流转发，
// 让阶段进度能被前端看到，同时补齐鉴权、限流与请求体上限。
func handleRetrievalStream(w http.ResponseWriter, r *http.Request) {
	if cfg.retrievalURL == nil {
		writeJSON(w, http.StatusServiceUnavailable, map[string]string{"error": "检索服务未配置"})
		return
	}

	// 先限制请求体再构造上游请求：超限时 Read 会立刻报错，不会先被读进内存
	body := http.MaxBytesReader(w, r.Body, maxStreamBodyBytes)
	req, err := http.NewRequestWithContext(r.Context(), http.MethodPost,
		cfg.retrievalURL.String()+"/stream", body)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "构造转发请求失败"})
		return
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Accept", "text/event-stream")

	// 不设 Client.Timeout：SSE 是长连接，超时由客户端断开与上游 ctx 决定。
	// ReadHeaderTimeout 在 transport 层已经兜住「连上了但迟迟不响应头」。
	resp, err := streamClient.Do(req)
	if err != nil {
		logger.Warn("检索流式服务不可达", "err", err)
		writeJSON(w, http.StatusBadGateway, map[string]string{"error": "检索服务暂不可用"})
		return
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		// 上游的错误响应（如参数校验失败）原样回给客户端，不要包装成 502
		w.Header().Set("Content-Type", resp.Header.Get("Content-Type"))
		w.WriteHeader(resp.StatusCode)
		_, _ = io.Copy(w, io.LimitReader(resp.Body, maxStreamBodyBytes))
		return
	}

	w.Header().Set("Content-Type", "text/event-stream; charset=utf-8")
	w.Header().Set("Cache-Control", "no-cache")
	w.Header().Set("Connection", "keep-alive")
	// 让反向代理不要攒缓冲，否则客户端会「攒一大块才收到」
	w.Header().Set("X-Accel-Buffering", "no")
	w.WriteHeader(http.StatusOK)

	flusher, _ := w.(http.Flusher)
	buf := make([]byte, 4096)
	for {
		n, readErr := resp.Body.Read(buf)
		if n > 0 {
			if _, writeErr := w.Write(buf[:n]); writeErr != nil {
				return // 客户端已断开
			}
			if flusher != nil {
				flusher.Flush()
			}
		}
		if readErr != nil {
			return
		}
	}
}

// ---------------------------------------------------------------------------
// 入口鉴权
// ---------------------------------------------------------------------------

// ctxKey 是本包私有的 context 键类型，避免与其它包的同名字符串键冲突。
type ctxKey int

const userCtxKey ctxKey = iota

// tokenClaims 是校验结果的内部表示：只取网关用得上的字段。
type tokenClaims struct {
	Valid   bool
	Subject string
}

// withAuth 在入口做一次令牌校验，把用户身份放进 context 供日志与限流使用。
//
// 三档行为，都不阻断主流程：
//   - 未配置业务服务：不接管，Python 自己验（保持单服务部署时的原行为）；
//   - 无令牌：匿名放行（对话接口允许匿名）；
//   - 有令牌：调业务服务验签。成功注入用户身份；失败或服务不可用只记录，
//     仍按匿名放行 —— 鉴权服务抖动不该变成业务整体不可用。
func withAuth(next http.Handler) http.Handler {
	if cfg.businessURL == nil {
		return next
	}
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		token := bearerToken(r)
		if token == "" {
			next.ServeHTTP(w, r)
			return
		}

		claims, err := verifyToken(r.Context(), token)
		switch {
		case err != nil:
			logger.Warn("入口鉴权校验不可用，按匿名继续", "err", err, "path", r.URL.Path)
			w.Header().Set("X-Gateway-Auth", "unavailable")
		case !claims.Valid:
			// 无效令牌不在这里拒绝：匿名可访问的接口仍应正常工作，
			// 需要登录的接口由 Python 统一返回 401，错误格式才不会出现两套。
			w.Header().Set("X-Gateway-Auth", "invalid")
		default:
			w.Header().Set("X-Gateway-Auth", "valid")
			r = r.WithContext(context.WithValue(r.Context(), userCtxKey, claims.Subject))
			logger.Info("入口鉴权通过", "user_id", claims.Subject, "path", r.URL.Path)
		}
		next.ServeHTTP(w, r)
	})
}

// verifyToken 调业务服务校验令牌，并用 singleflight 合并同一令牌的并发校验。
//
// 同一用户并发打开多个面板时，会有多个请求带同一令牌进来；没有合并的话
// 每个请求都要多打一次业务服务，形成无意义的放大。key 用令牌摘要而不是
// 令牌原文：singleflight 的 key 会留在内存里，不该放凭据明文。
func verifyToken(ctx context.Context, token string) (tokenClaims, error) {
	sum := sha256.Sum256([]byte(token))
	key := hex.EncodeToString(sum[:16])

	return singleflight.Do(authGroup, ctx, key, func(ctx context.Context) (tokenClaims, error) {
		req, err := http.NewRequestWithContext(ctx, http.MethodGet,
			cfg.businessURL.String()+"/api/auth/verify", nil)
		if err != nil {
			return tokenClaims{}, err
		}
		req.Header.Set("Authorization", "Bearer "+token)

		resp, err := businessHTTP.Do(req)
		if err != nil {
			return tokenClaims{}, err
		}
		defer resp.Body.Close()
		if resp.StatusCode != http.StatusOK {
			return tokenClaims{}, fmt.Errorf("校验服务返回 %d", resp.StatusCode)
		}

		var payload struct {
			Valid  bool `json:"valid"`
			Claims struct {
				Sub string `json:"sub"`
			} `json:"claims"`
		}
		if err := json.NewDecoder(io.LimitReader(resp.Body, 64<<10)).Decode(&payload); err != nil {
			return tokenClaims{}, err
		}
		return tokenClaims{Valid: payload.Valid, Subject: payload.Claims.Sub}, nil
	})
}

// bearerToken 取 Authorization 头里的令牌；兼容不带 Bearer 前缀的裸令牌。
func bearerToken(r *http.Request) string {
	raw := strings.TrimSpace(r.Header.Get("Authorization"))
	if raw == "" {
		return ""
	}
	if len(raw) > 7 && strings.EqualFold(raw[:7], "bearer ") {
		return strings.TrimSpace(raw[7:])
	}
	return raw
}

// ratelimitKey 决定限流分桶维度：优先按已鉴权用户，其次按来源 IP。
//
// 匿名请求无法稳定识别个体，按 IP 是最接近的成本归属方式；
// 这也和 Python 侧固定窗口限流的口径一致（有身份按用户，否则共享一个桶）。
func ratelimitKey(r *http.Request) string {
	if id, ok := r.Context().Value(userCtxKey).(string); ok && id != "" {
		return "user:" + id
	}
	host, _, err := net.SplitHostPort(r.RemoteAddr)
	if err != nil {
		host = r.RemoteAddr
	}
	return "ip:" + host
}

// ---------------------------------------------------------------------------
// 反向代理
// ---------------------------------------------------------------------------

// newBackendProxy 构造指向 Python 后端的反向代理。
//
// FlushInterval = -1 是关键：默认行为会把响应攒起来再写，SSE 就变成
// 「等全部生成完才一次性到达」。设为 -1 表示每次写入都立刻 flush。
func newBackendProxy(target *url.URL) *httputil.ReverseProxy {
	p := httputil.NewSingleHostReverseProxy(target)
	p.FlushInterval = -1
	p.Transport = newTransport(true)
	p.ErrorHandler = func(w http.ResponseWriter, r *http.Request, err error) {
		logger.Error("后端不可达", "path", r.URL.Path, "err", err)
		w.Header().Set("Content-Type", "application/json; charset=utf-8")
		w.WriteHeader(http.StatusBadGateway)
		_, _ = w.Write(emptyResponse)
	}
	p.ModifyResponse = func(resp *http.Response) error {
		// 标记响应来自 Go 网关，便于排查「请求到底走了哪条链路」
		resp.Header.Set("X-Gateway", "go")
		return nil
	}
	return p
}

// newTransport 返回一份连接池配置，避免使用 http.DefaultTransport 的全局单例。
//
// 显式禁用代理：目标是本机/内网服务，若跟随 HTTP_PROXY 环境变量，
// 本地请求会被绕到代理上，表现为「连接超时但服务其实是好的」。
func newTransport(limitHeaderWait bool) *http.Transport {
	t := &http.Transport{
		Proxy:               nil,
		DialContext:         (&net.Dialer{Timeout: 5 * time.Second, KeepAlive: 30 * time.Second}).DialContext,
		MaxIdleConns:        100,
		MaxIdleConnsPerHost: 20,
		IdleConnTimeout:     90 * time.Second,
		ForceAttemptHTTP2:   false,
	}
	if limitHeaderWait {
		// 只限制「等到响应头」的时间，不限制流式 body 的持续时间，
		// 因此对 SSE 安全：上游迟迟不开始响应才会被掐断。
		t.ResponseHeaderTimeout = cfg.upstreamTimeout
	}
	return t
}

// withCORS 只处理预检请求，实际请求的 CORS 头交给 Python 返回。
//
// 若两层都设置 Access-Control-Allow-Origin，反代会把它们合并成两个值，
// 浏览器会直接判定跨域失败。所以这里不重复设置，只在 OPTIONS 时短路。
func withCORS(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodOptions {
			next.ServeHTTP(w, r)
			return
		}
		w.Header().Set("Access-Control-Allow-Origin", "*")
		w.Header().Set("Access-Control-Allow-Methods", "GET, POST, PUT, PATCH, DELETE, OPTIONS")
		w.Header().Set("Access-Control-Allow-Headers", "Authorization, Content-Type, X-Request-ID")
		w.Header().Set("Access-Control-Max-Age", "600")
		w.WriteHeader(http.StatusNoContent)
	})
}

// ---------------------------------------------------------------------------
// 配置与工具
// ---------------------------------------------------------------------------

func loadConfig() config {
	c := config{
		addr:            envString("ADDR", defaultAddr),
		upstreamTimeout: envDuration("GATEWAY_UPSTREAM_TIMEOUT", 30*time.Second),
		rateLimit:       envFloat("GATEWAY_RATE_LIMIT", 0),
		pprof:           envBool("GATEWAY_PPROF", false),
	}

	backend, err := url.Parse(envString("BACKEND_URL", "http://127.0.0.1:8000"))
	if err != nil {
		fatalf("BACKEND_URL 不是合法地址: %v", err)
	}
	c.backendURL = backend

	c.retrievalURL = optionalURL("RETRIEVAL_URL")
	c.businessURL = optionalURL("GO_BUSINESS_URL")
	c.frontendDir = strings.TrimSpace(os.Getenv("GATEWAY_FRONTEND_DIR"))
	c.goPendingTickets = envBool("GATEWAY_GO_PENDING_TICKETS", false)
	c.goTicketWrites = envBool("GATEWAY_GO_TICKET_WRITES", false)

	c.rateBurst = envInt("GATEWAY_RATE_BURST", int(c.rateLimit))
	if c.rateLimit > 0 && c.rateBurst <= 0 {
		c.rateBurst = 1
	}
	return c
}

// optionalURL 读一个可选的服务地址；空值表示不启用该能力。
func optionalURL(env string) *url.URL {
	raw := envString(env, "")
	if raw == "" {
		return nil
	}
	u, err := url.Parse(raw)
	if err != nil {
		fatalf("%s 不是合法地址: %v", env, err)
	}
	return u
}

func fetchJSON(ctx context.Context, rawURL string) (map[string]any, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, rawURL, nil)
	if err != nil {
		return nil, err
	}
	resp, err := businessHTTP.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("状态码 %d", resp.StatusCode)
	}
	var out map[string]any
	if err := json.NewDecoder(io.LimitReader(resp.Body, 64<<10)).Decode(&out); err != nil {
		return nil, err
	}
	return out, nil
}

func writeJSON(w http.ResponseWriter, code int, payload any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(code)
	if err := json.NewEncoder(w).Encode(payload); err != nil {
		logger.Error("响应编码失败", "err", err)
	}
}

func urlOrEmpty(u *url.URL) string {
	if u == nil {
		return ""
	}
	return u.String()
}

func envString(name, fallback string) string {
	if v := strings.TrimSpace(os.Getenv(name)); v != "" {
		return v
	}
	return fallback
}

func envInt(name string, fallback int) int {
	raw := strings.TrimSpace(os.Getenv(name))
	if raw == "" {
		return fallback
	}
	v, err := strconv.Atoi(raw)
	if err != nil {
		logger.Warn("配置不是整数，使用默认值", "name", name, "value", raw, "fallback", fallback)
		return fallback
	}
	return v
}

func envFloat(name string, fallback float64) float64 {
	raw := strings.TrimSpace(os.Getenv(name))
	if raw == "" {
		return fallback
	}
	v, err := strconv.ParseFloat(raw, 64)
	if err != nil || v < 0 {
		logger.Warn("配置不是非负浮点数，使用默认值", "name", name, "value", raw, "fallback", fallback)
		return fallback
	}
	return v
}

func envDuration(name string, fallback time.Duration) time.Duration {
	raw := strings.TrimSpace(os.Getenv(name))
	if raw == "" {
		return fallback
	}
	v, err := time.ParseDuration(raw)
	if err != nil || v <= 0 {
		logger.Warn("配置不是合法时长，使用默认值", "name", name, "value", raw, "fallback", fallback)
		return fallback
	}
	return v
}

func envBool(name string, fallback bool) bool {
	raw := strings.TrimSpace(os.Getenv(name))
	if raw == "" {
		return fallback
	}
	v, err := strconv.ParseBool(raw)
	if err != nil {
		logger.Warn("配置不是布尔值，使用默认值", "name", name, "value", raw, "fallback", fallback)
		return fallback
	}
	return v
}

func fatalf(format string, args ...any) {
	slog.Error(fmt.Sprintf(format, args...))
	os.Exit(1)
}
