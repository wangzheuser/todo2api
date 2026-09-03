package upstream

import (
	"bufio"
	"context"
	"crypto/tls"
	"crypto/x509"
	"encoding/json"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/gorilla/websocket"
)

func TestFrontendWSURL(t *testing.T) {
	got, err := frontendWSURL("https://api.todofor.ai/api/v1", "tab-1")
	if err != nil {
		t.Fatal(err)
	}
	if got != "wss://api.todofor.ai/ws/v1/frontend?tabId=tab-1" {
		t.Fatalf("URL = %q", got)
	}
}

func TestHTTPSProxyDialCreatesAuthenticatedTunnel(t *testing.T) {
	target, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer target.Close()
	go func() {
		connection, acceptErr := target.Accept()
		if acceptErr != nil {
			return
		}
		defer connection.Close()
		buffer := make([]byte, 4)
		_, _ = io.ReadFull(connection, buffer)
		if string(buffer) == "ping" {
			_, _ = connection.Write([]byte("pong"))
		}
	}()

	proxy := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodConnect || r.Host != target.Addr().String() {
			t.Errorf("CONNECT method=%s host=%s", r.Method, r.Host)
			http.Error(w, "bad tunnel", http.StatusBadRequest)
			return
		}
		if r.Header.Get("Proxy-Authorization") != "Basic dXNlcjpwYXNz" {
			t.Errorf("Proxy-Authorization=%q", r.Header.Get("Proxy-Authorization"))
		}
		upstream, dialErr := net.Dial("tcp", r.Host)
		if dialErr != nil {
			http.Error(w, dialErr.Error(), http.StatusBadGateway)
			return
		}
		hijacker, ok := w.(http.Hijacker)
		if !ok {
			t.Error("proxy response does not support hijacking")
			upstream.Close()
			return
		}
		client, buffered, hijackErr := hijacker.Hijack()
		if hijackErr != nil {
			t.Error(hijackErr)
			upstream.Close()
			return
		}
		defer client.Close()
		defer upstream.Close()
		_, _ = buffered.WriteString("HTTP/1.1 200 Connection Established\r\n\r\n")
		_ = buffered.Flush()
		go func() { _, _ = io.Copy(upstream, client) }()
		_, _ = io.Copy(client, upstream)
	}))
	defer proxy.Close()

	proxyURL, _ := url.Parse(strings.Replace(proxy.URL, "https://", "https://user:pass@", 1))
	roots := x509.NewCertPool()
	roots.AddCert(proxy.Certificate())
	dial := httpsProxyDialContextWithConfig(proxyURL, &tls.Config{RootCAs: roots})
	connection, err := dial(context.Background(), "tcp", target.Addr().String())
	if err != nil {
		t.Fatal(err)
	}
	defer connection.Close()
	if _, err := connection.Write([]byte("ping")); err != nil {
		t.Fatal(err)
	}
	response, err := bufio.NewReader(connection).ReadString('g')
	if err != nil || response != "pong" {
		t.Fatalf("response=%q err=%v", response, err)
	}
}

func TestFrontendSubscriptionProtocol(t *testing.T) {
	const apiKey = "test-api-key"
	var (
		mu          sync.Mutex
		connections = map[string]*websocket.Conn{}
	)
	upgrader := websocket.Upgrader{
		CheckOrigin:  func(*http.Request) bool { return true },
		Subprotocols: []string{apiKey},
	}
	mux := http.NewServeMux()
	mux.HandleFunc("/ws/v1/frontend", func(w http.ResponseWriter, r *http.Request) {
		tabID := r.URL.Query().Get("tabId")
		if tabID == "" {
			t.Error("missing WebSocket tabId")
		}
		if !strings.Contains(r.Header.Get("Sec-WebSocket-Protocol"), apiKey) {
			t.Errorf("WebSocket subprotocol = %q", r.Header.Get("Sec-WebSocket-Protocol"))
		}
		conn, err := upgrader.Upgrade(w, r, nil)
		if err != nil {
			t.Error(err)
			return
		}
		mu.Lock()
		connections[tabID] = conn
		mu.Unlock()
	})
	mux.HandleFunc("/api/v1/todos/todo-1/subscribe", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			t.Errorf("method = %s", r.Method)
		}
		if r.Header.Get("X-API-Key") != apiKey {
			t.Errorf("X-API-Key = %q", r.Header.Get("X-API-Key"))
		}
		tabID := r.Header.Get("X-Tab-ID")
		var body subscribeTodoReq
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Error(err)
		}
		if body.TodoID != "todo-1" {
			t.Errorf("body todoId = %q", body.TodoID)
		}
		mu.Lock()
		conn := connections[tabID]
		mu.Unlock()
		if conn == nil {
			t.Errorf("no WebSocket for tab %q", tabID)
			http.Error(w, "missing socket", http.StatusConflict)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(map[string]any{"id": "todo-1", "status": "RUNNING"})
		go func() {
			conn.WriteJSON(map[string]any{
				"type":    "block:message",
				"payload": map[string]any{"todoId": "todo-1", "content": "hello"},
			})
			conn.WriteJSON(map[string]any{
				"type":    "todo:status",
				"payload": map[string]any{"todoId": "todo-1", "status": "READY"},
			})
		}()
	})
	server := httptest.NewServer(mux)
	defer server.Close()

	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	client := New(server.URL+"/api/v1", apiKey)
	sub, err := client.PrepareSubscription(ctx)
	if err != nil {
		t.Fatal(err)
	}
	defer sub.Close()

	events := make(chan Event, 2)
	errCh := make(chan error, 1)
	go func() { errCh <- sub.Subscribe(ctx, "todo-1", events) }()

	for _, want := range []string{"block:message", "todo:status"} {
		select {
		case ev := <-events:
			if ev.Type != want {
				t.Fatalf("event type = %q, want %q", ev.Type, want)
			}
		case err := <-errCh:
			t.Fatalf("subscription ended early: %v", err)
		case <-ctx.Done():
			t.Fatal("timed out waiting for event")
		}
	}
}
