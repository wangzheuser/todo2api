package upstream

import (
	"bufio"
	"bytes"
	"context"
	"crypto/rand"
	"crypto/tls"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"strings"
	"sync"
	"time"

	"github.com/gorilla/websocket"
)

// Event is one frontend WebSocket envelope from todofor.ai.
type Event struct {
	Type    string          `json:"type"`
	Payload json.RawMessage `json:"payload"`
}

// Subscription is a connected frontend WebSocket. It is intentionally opened
// before creating or resuming a todo so early run events cannot be missed.
type Subscription struct {
	client    *Client
	tabID     string
	conn      *websocket.Conn
	closed    chan struct{}
	closeOnce sync.Once
}

// PrepareSubscription opens the shared frontend WebSocket. The API key is sent
// as the WebSocket subprotocol, matching the official todofor.ai CLI.
func (c *Client) PrepareSubscription(ctx context.Context) (*Subscription, error) {
	tabID, err := newTabID()
	if err != nil {
		return nil, err
	}
	wsEndpoint, err := frontendWSURL(c.baseURL, tabID)
	if err != nil {
		return nil, err
	}

	var conn *websocket.Conn
	var resp *http.Response
	if c.proxies != nil {
		for _, candidate := range c.proxies.Candidates(c.accountID, 2) {
			conn, resp, err = c.dialFrontend(ctx, wsEndpoint, candidate.URL)
			if err == nil {
				c.proxies.MarkSucceeded(c.accountID, candidate.Key)
				break
			}
			if resp != nil && resp.StatusCode != http.StatusProxyAuthRequired {
				break
			}
			if resp != nil {
				resp.Body.Close()
			}
			c.proxies.MarkFailed(c.accountID, candidate.Key)
			resp = nil
		}
	}
	if conn == nil && resp == nil {
		conn, resp, err = c.dialFrontend(ctx, wsEndpoint, nil)
	}
	if err != nil {
		if resp != nil {
			data, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
			resp.Body.Close()
			return nil, newHTTPError(http.MethodGet, wsEndpoint, resp.StatusCode, data)
		}
		return nil, fmt.Errorf("frontend ws dial %s: %w", wsEndpoint, err)
	}
	conn.SetReadLimit(5 * 1024 * 1024)

	sub := &Subscription{
		client: c,
		tabID:  tabID,
		conn:   conn,
		closed: make(chan struct{}),
	}
	go func() {
		select {
		case <-ctx.Done():
			sub.Close()
		case <-sub.closed:
		}
	}()
	return sub, nil
}

func (c *Client) dialFrontend(ctx context.Context, endpoint string, proxyURL *url.URL) (*websocket.Conn, *http.Response, error) {
	dialer := websocket.Dialer{
		HandshakeTimeout: 15 * time.Second,
		Subprotocols:     []string{c.apiKey},
	}
	if proxyURL != nil {
		if proxyURL.Scheme == "https" {
			dialer.NetDialContext = httpsProxyDialContext(proxyURL)
		} else {
			dialer.Proxy = http.ProxyURL(proxyURL)
		}
	}
	return dialer.DialContext(ctx, endpoint, nil)
}

func httpsProxyDialContext(proxyURL *url.URL) func(context.Context, string, string) (net.Conn, error) {
	return httpsProxyDialContextWithConfig(proxyURL, nil)
}

func httpsProxyDialContextWithConfig(proxyURL *url.URL, baseTLS *tls.Config) func(context.Context, string, string) (net.Conn, error) {
	return func(ctx context.Context, network, target string) (net.Conn, error) {
		proxyAddress := proxyURL.Host
		if proxyURL.Port() == "" {
			proxyAddress = net.JoinHostPort(proxyURL.Hostname(), "443")
		}
		plain, err := (&net.Dialer{}).DialContext(ctx, network, proxyAddress)
		if err != nil {
			return nil, err
		}
		config := &tls.Config{MinVersion: tls.VersionTLS12}
		if baseTLS != nil {
			config = baseTLS.Clone()
			config.MinVersion = tls.VersionTLS12
		}
		config.ServerName = proxyURL.Hostname()
		tunnel := tls.Client(plain, config)
		if err := tunnel.HandshakeContext(ctx); err != nil {
			plain.Close()
			return nil, err
		}
		header := make(http.Header)
		if proxyURL.User != nil {
			password, _ := proxyURL.User.Password()
			credentials := base64.StdEncoding.EncodeToString([]byte(proxyURL.User.Username() + ":" + password))
			header.Set("Proxy-Authorization", "Basic "+credentials)
		}
		request := &http.Request{Method: http.MethodConnect, URL: &url.URL{Opaque: target}, Host: target, Header: header}
		if err := request.Write(tunnel); err != nil {
			tunnel.Close()
			return nil, err
		}
		response, err := http.ReadResponse(bufio.NewReader(tunnel), request)
		if err != nil {
			tunnel.Close()
			return nil, err
		}
		response.Body.Close()
		if response.StatusCode != http.StatusOK {
			tunnel.Close()
			return nil, fmt.Errorf("https proxy CONNECT failed: %s", response.Status)
		}
		return tunnel, nil
	}
}

// Subscribe is a convenience method for callers that do not need to connect
// before starting the upstream run.
func (c *Client) Subscribe(ctx context.Context, todoID string, out chan<- Event) error {
	sub, err := c.PrepareSubscription(ctx)
	if err != nil {
		return err
	}
	defer sub.Close()
	return sub.Subscribe(ctx, todoID, out)
}

// Subscribe binds this frontend tab to todoID over HTTP, then forwards its
// WebSocket envelopes until the context is canceled or the socket closes.
func (s *Subscription) Subscribe(ctx context.Context, todoID string, out chan<- Event) error {
	if err := s.client.subscribeTodo(ctx, todoID, s.tabID); err != nil {
		return err
	}
	for {
		_, data, err := s.conn.ReadMessage()
		if err != nil {
			if ctx.Err() != nil {
				return ctx.Err()
			}
			return fmt.Errorf("frontend ws read: %w", err)
		}
		var ev Event
		if err := json.Unmarshal(data, &ev); err != nil || ev.Type == "" {
			continue
		}
		if err := sendEvent(ctx, out, ev); err != nil {
			return err
		}
	}
}

func (s *Subscription) Close() {
	s.closeOnce.Do(func() {
		close(s.closed)
		s.conn.Close()
	})
}

type subscribeTodoReq struct {
	TodoID string `json:"todoId"`
}

func (c *Client) subscribeTodo(ctx context.Context, todoID, tabID string) error {
	body, err := json.Marshal(subscribeTodoReq{TodoID: todoID})
	if err != nil {
		return err
	}
	path := fmt.Sprintf("/todos/%s/subscribe", url.PathEscape(todoID))
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.baseURL+path, bytes.NewReader(body))
	if err != nil {
		return err
	}
	req.Header.Set("X-API-Key", c.apiKey)
	req.Header.Set("X-Tab-ID", tabID)
	req.Header.Set("Content-Type", "application/json")

	resp, err := c.http.Do(req)
	if err != nil {
		return fmt.Errorf("subscribe todo: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode >= http.StatusMultipleChoices {
		data, err := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
		if err != nil {
			return err
		}
		return newHTTPError(http.MethodPost, path, resp.StatusCode, data)
	}
	_, err = io.Copy(io.Discard, resp.Body)
	return err
}

func frontendWSURL(baseURL, tabID string) (string, error) {
	u, err := url.Parse(baseURL)
	if err != nil {
		return "", fmt.Errorf("parse upstream base URL: %w", err)
	}
	switch u.Scheme {
	case "https":
		u.Scheme = "wss"
	case "http":
		u.Scheme = "ws"
	default:
		return "", fmt.Errorf("unsupported upstream URL scheme %q", u.Scheme)
	}
	u.Path = strings.TrimSuffix(strings.TrimRight(u.Path, "/"), "/api/v1") + "/ws/v1/frontend"
	u.RawPath = ""
	q := u.Query()
	q.Set("tabId", tabID)
	u.RawQuery = q.Encode()
	return u.String(), nil
}

func newTabID() (string, error) {
	var id [16]byte
	if _, err := rand.Read(id[:]); err != nil {
		return "", fmt.Errorf("generate frontend tab id: %w", err)
	}
	id[6] = (id[6] & 0x0f) | 0x40
	id[8] = (id[8] & 0x3f) | 0x80
	return fmt.Sprintf("%x-%x-%x-%x-%x", id[0:4], id[4:6], id[6:8], id[8:10], id[10:16]), nil
}

func sendEvent(ctx context.Context, out chan<- Event, ev Event) error {
	select {
	case out <- ev:
		return nil
	case <-ctx.Done():
		return ctx.Err()
	}
}
