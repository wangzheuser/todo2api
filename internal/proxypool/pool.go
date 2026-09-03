package proxypool

import (
	"bytes"
	"crypto/sha256"
	"fmt"
	"net/http"
	"net/url"
	"sort"
	"strings"
	"sync"
	"time"
)

// Candidate is one proxy route and its shared HTTP transport.
type Candidate struct {
	Key       string
	URL       *url.URL
	Transport http.RoundTripper
}

type accountState struct {
	preferred string
	failed    map[string]struct{}
}

// Pool keeps the configured proxies and process-local per-account affinity.
type Pool struct {
	mu         sync.Mutex
	urls       []string
	parsed     map[string]*url.URL
	transports map[string]*http.Transport
	accounts   map[string]*accountState
}

// Parse validates and normalizes one proxy URL per non-empty line.
func Parse(value string) ([]string, error) {
	seen := make(map[string]struct{})
	proxies := make([]string, 0)
	for index, line := range strings.Split(strings.ReplaceAll(value, "\r\n", "\n"), "\n") {
		line = strings.TrimSpace(line)
		if line == "" {
			continue
		}
		normalized, err := normalize(line)
		if err != nil {
			return nil, fmt.Errorf("第 %d 行代理地址无效: %w", index+1, err)
		}
		if _, exists := seen[normalized]; exists {
			continue
		}
		seen[normalized] = struct{}{}
		proxies = append(proxies, normalized)
	}
	return proxies, nil
}

func normalize(value string) (string, error) {
	u, err := url.Parse(value)
	if err != nil {
		return "", fmt.Errorf("格式错误")
	}
	u.Scheme = strings.ToLower(u.Scheme)
	switch u.Scheme {
	case "http", "https":
	default:
		return "", fmt.Errorf("仅支持 http 和 https")
	}
	if u.Hostname() == "" {
		return "", fmt.Errorf("缺少主机名")
	}
	if u.Path != "" && u.Path != "/" {
		return "", fmt.Errorf("不能包含路径")
	}
	if u.RawQuery != "" || u.Fragment != "" {
		return "", fmt.Errorf("不能包含查询参数或片段")
	}
	u.Path = ""
	u.RawPath = ""
	u.Host = strings.ToLower(u.Host)
	return u.String(), nil
}

// New constructs a proxy pool from an already validated list.
func New(proxies []string) (*Pool, error) {
	p := &Pool{
		parsed: make(map[string]*url.URL), transports: make(map[string]*http.Transport),
		accounts: make(map[string]*accountState),
	}
	if err := p.Replace(proxies); err != nil {
		return nil, err
	}
	return p, nil
}

// Replace atomically installs the whole proxy list and resets affinity only
// when the normalized configuration actually changes.
func (p *Pool) Replace(proxies []string) error {
	parsed := make(map[string]*url.URL, len(proxies))
	normalized := make([]string, 0, len(proxies))
	for _, raw := range proxies {
		value, err := normalize(raw)
		if err != nil {
			return err
		}
		if _, exists := parsed[value]; exists {
			continue
		}
		u, _ := url.Parse(value)
		parsed[value] = u
		normalized = append(normalized, value)
	}

	p.mu.Lock()
	defer p.mu.Unlock()
	if sameSet(p.urls, normalized) {
		return nil
	}
	oldTransports := p.transports
	p.urls = normalized
	p.parsed = parsed
	p.transports = make(map[string]*http.Transport, len(normalized))
	for _, value := range normalized {
		if transport := oldTransports[value]; transport != nil {
			p.transports[value] = transport
			delete(oldTransports, value)
			continue
		}
		p.transports[value] = newTransport(parsed[value])
	}
	for _, transport := range oldTransports {
		transport.CloseIdleConnections()
	}
	p.accounts = make(map[string]*accountState)
	return nil
}

func sameSet(left, right []string) bool {
	if len(left) != len(right) {
		return false
	}
	values := make(map[string]struct{}, len(left))
	for _, value := range left {
		values[value] = struct{}{}
	}
	for _, value := range right {
		if _, exists := values[value]; !exists {
			return false
		}
	}
	return true
}

func newTransport(proxyURL *url.URL) *http.Transport {
	transport := http.DefaultTransport.(*http.Transport).Clone()
	transport.Proxy = http.ProxyURL(proxyURL)
	transport.MaxIdleConns = 64
	transport.MaxIdleConnsPerHost = 32
	transport.MaxConnsPerHost = 64
	transport.IdleConnTimeout = 90 * time.Second
	transport.TLSHandshakeTimeout = 10 * time.Second
	transport.ExpectContinueTimeout = time.Second
	transport.ForceAttemptHTTP2 = true
	return transport
}

// Len returns the number of configured proxies.
func (p *Pool) Len() int {
	p.mu.Lock()
	defer p.mu.Unlock()
	return len(p.urls)
}

// Candidates returns at most limit sticky proxy attempts for one account.
func (p *Pool) Candidates(account string, limit int) []Candidate {
	if limit <= 0 {
		return nil
	}
	p.mu.Lock()
	defer p.mu.Unlock()
	if len(p.urls) == 0 {
		return nil
	}
	state := p.accounts[account]
	if state == nil {
		state = &accountState{failed: make(map[string]struct{})}
		p.accounts[account] = state
	}
	ordered := append([]string(nil), p.urls...)
	sort.Slice(ordered, func(i, j int) bool {
		left := routeScore(account, ordered[i])
		right := routeScore(account, ordered[j])
		return bytes.Compare(left[:], right[:]) > 0
	})
	if state.preferred != "" {
		for i, value := range ordered {
			if value == state.preferred {
				copy(ordered[1:i+1], ordered[:i])
				ordered[0] = value
				break
			}
		}
	}
	result := make([]Candidate, 0, min(limit, len(ordered)))
	for _, value := range ordered {
		if _, failed := state.failed[value]; failed {
			continue
		}
		result = append(result, Candidate{
			Key: value, URL: cloneURL(p.parsed[value]), Transport: p.transports[value],
		})
		if len(result) == limit {
			break
		}
	}
	return result
}

func routeScore(account, proxy string) [sha256.Size]byte {
	return sha256.Sum256([]byte(account + "\x00" + proxy))
}

func cloneURL(value *url.URL) *url.URL {
	if value == nil {
		return nil
	}
	copy := *value
	return &copy
}

// MarkFailed excludes a proxy only for the current account.
func (p *Pool) MarkFailed(account, proxy string) {
	p.mu.Lock()
	defer p.mu.Unlock()
	state := p.accounts[account]
	if state == nil {
		state = &accountState{failed: make(map[string]struct{})}
		p.accounts[account] = state
	}
	state.failed[proxy] = struct{}{}
	if state.preferred == proxy {
		state.preferred = ""
	}
}

// MarkSucceeded makes a working fallback sticky for the current process.
func (p *Pool) MarkSucceeded(account, proxy string) {
	p.mu.Lock()
	defer p.mu.Unlock()
	state := p.accounts[account]
	if state == nil {
		state = &accountState{failed: make(map[string]struct{})}
		p.accounts[account] = state
	}
	state.preferred = proxy
}
