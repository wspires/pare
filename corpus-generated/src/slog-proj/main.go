// An order service under load, logging with log/slog: JSON lines
// (-format json) or logfmt (-format text). It serves real HTTP requests
// through net/http/httptest and fails in chosen ways:
//
//   - the inventory database refuses connections for a stretch of requests
//     (one error shape, repeated, with retries logged as warnings);
//   - one request hits a nil pointer, recovered by middleware that logs
//     the panic and its stack trace as a single attribute;
//   - a payment call times out once (context deadline);
//   - slow requests are warnings.
//
// Everything is seeded, so a run is reproducible apart from timestamps
// (fixed too, by a clock that advances per request).
package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"io"
	"log/slog"
	"math/rand"
	"net/http"
	"net/http/httptest"
	"os"
	"runtime/debug"
	"strings"
	"time"
)

var clock = time.Date(2026, 10, 7, 9, 0, 0, 0, time.UTC)

type order struct {
	ID    int
	Items []string
	Card  *string
}

var (
	rng          = rand.New(rand.NewSource(7))
	dbDown       = false
	errRefused   = errors.New("dial tcp 10.0.4.17:5432: connect: connection refused")
	paymentSlow  = 0
	served, fail int
)

func reserve(ctx context.Context, log *slog.Logger, o *order) error {
	for attempt := 1; attempt <= 3; attempt++ {
		if !dbDown {
			return nil
		}
		log.WarnContext(ctx, "inventory reserve failed, retrying", "order_id", o.ID, "attempt", attempt, "err", errRefused)
	}
	return fmt.Errorf("inventory: reserve order %d: %w", o.ID, errRefused)
}

func charge(ctx context.Context, o *order) error {
	if paymentSlow > 0 {
		paymentSlow--
		ctx, cancel := context.WithTimeout(ctx, time.Millisecond)
		defer cancel()
		<-ctx.Done()
		return fmt.Errorf("payment: charge order %d: %w", o.ID, ctx.Err())
	}
	if o.Card == nil {
		_ = len(*o.Card) // the chosen bug: a guest checkout has no card on file
	}
	return nil
}

type ctxKey struct{}

func handler(log *slog.Logger) http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("POST /orders/{id}", func(w http.ResponseWriter, r *http.Request) {
		var id int
		fmt.Sscan(r.PathValue("id"), &id)
		card := "visa"
		o := &order{ID: id, Items: []string{"sku-1"}, Card: &card}
		if id == 1337 {
			o.Card = nil
		}
		ctx := r.Context()
		l := log.With("req_id", r.Header.Get("X-Request-Id"), "order_id", id)
		if err := reserve(ctx, l, o); err != nil {
			l.ErrorContext(ctx, "order failed", "err", err)
			http.Error(w, "unavailable", http.StatusServiceUnavailable)
			return
		}
		if err := charge(ctx, o); err != nil {
			l.ErrorContext(ctx, "order failed", "err", err)
			http.Error(w, "timeout", http.StatusGatewayTimeout)
			return
		}
		w.WriteHeader(http.StatusCreated)
	})
	mux.HandleFunc("GET /orders/{id}", func(w http.ResponseWriter, r *http.Request) {
		io.WriteString(w, `{"status":"shipped"}`)
	})
	return mux
}

// middleware logs each request and recovers panics.
func middleware(log *slog.Logger, next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		rec := &statusRecorder{ResponseWriter: w, status: 200}
		dur := time.Duration(rng.Intn(40)+2) * time.Millisecond
		if rng.Intn(60) == 0 {
			dur = time.Duration(rng.Intn(3000)+1200) * time.Millisecond
		}
		defer func() {
			if p := recover(); p != nil {
				stack := string(debug.Stack())
				// Keep the trace stable between machines: drop goroutine ids and offsets.
				stack = scrub(stack)
				log.Error("panic recovered", "req_id", r.Header.Get("X-Request-Id"), "method", r.Method, "path", r.URL.Path, "panic", fmt.Sprint(p), "stack", stack)
				http.Error(w, "internal error", http.StatusInternalServerError)
				rec.status = 500
			}
			lvl := slog.LevelInfo
			if dur > time.Second {
				lvl = slog.LevelWarn
			}
			msg := "request"
			if lvl == slog.LevelWarn {
				msg = "slow request"
			}
			log.Log(r.Context(), lvl, msg, "req_id", r.Header.Get("X-Request-Id"), "method", r.Method, "path", r.URL.Path, "status", rec.status, "dur", dur, "bytes", rng.Intn(900)+40)
		}()
		next.ServeHTTP(rec, r)
	})
}

func scrub(s string) string {
	var out []string
	for _, l := range strings.Split(strings.TrimSpace(s), "\n") {
		if i := strings.Index(l, " +0x"); i >= 0 {
			l = l[:i]
		}
		if strings.HasPrefix(l, "goroutine ") {
			l = "goroutine 1 [running]:"
		}
		if strings.HasPrefix(l, "\t/") {
			if i := strings.Index(l, "/src/"); i >= 0 {
				l = "\t/usr/local/go" + l[i:]
			}
			if i := strings.Index(l, "slog-proj/"); i >= 0 {
				l = "\t/home/dev/proj/" + l[i+len("slog-proj/"):]
			}
		}
		out = append(out, l)
	}
	return strings.Join(out, "\n")
}

type statusRecorder struct {
	http.ResponseWriter
	status int
}

func (s *statusRecorder) WriteHeader(code int) { s.status = code; s.ResponseWriter.WriteHeader(code) }

func main() {
	format := flag.String("format", "json", "json or text")
	n := flag.Int("n", 2400, "requests")
	flag.Parse()
	opts := &slog.HandlerOptions{ReplaceAttr: func(groups []string, a slog.Attr) slog.Attr {
		if a.Key == slog.TimeKey {
			return slog.Time(slog.TimeKey, clock)
		}
		return a
	}}
	var h slog.Handler
	if *format == "json" {
		h = slog.NewJSONHandler(os.Stdout, opts)
	} else {
		h = slog.NewTextHandler(os.Stdout, opts)
	}
	log := slog.New(h).With("service", "orders")
	log.Info("starting", "version", "1.8.2", "addr", ":8080", "workers", 8)
	srv := httptest.NewServer(middleware(log, handler(log)))
	defer srv.Close()
	log.Info("connected to inventory db", "host", "10.0.4.17:5432", "pool", 20)
	client := srv.Client()
	for i := 0; i < *n; i++ {
		clock = clock.Add(time.Duration(rng.Intn(400)+50) * time.Millisecond)
		dbDown = i >= 1500 && i < 1540
		if i == 2000 {
			paymentSlow = 1
		}
		id := 1000 + rng.Intn(9000)
		if i == 1900 {
			id = 1337
		}
		method, path := "GET", fmt.Sprintf("/orders/%d", id)
		if rng.Intn(3) == 0 || i == 1900 || i == 2000 {
			method = "POST"
		}
		req, _ := http.NewRequest(method, srv.URL+path, nil)
		req.Header.Set("X-Request-Id", fmt.Sprintf("%016x", rng.Uint64()))
		resp, err := client.Do(req)
		if err != nil {
			log.Error("client", "err", err)
			continue
		}
		io.Copy(io.Discard, resp.Body)
		resp.Body.Close()
		served++
		if resp.StatusCode >= 500 {
			fail++
		}
		if i%500 == 499 {
			log.Info("stats", "served", served, "failed", fail, "pool_in_use", rng.Intn(20))
		}
	}
	log.Info("shutting down", "served", served, "failed", fail)
}
