package proxypool

import (
	"fmt"
	"reflect"
	"strings"
	"testing"
)

func TestParseNormalizesBlankLinesAndDuplicates(t *testing.T) {
	got, err := Parse(" HTTP://User:pass@Example.COM:8080 \n\nhttp://User:pass@example.com:8080\nhttps://proxy.test:443/\n")
	if err != nil {
		t.Fatal(err)
	}
	want := []string{"http://User:pass@example.com:8080", "https://proxy.test:443"}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("proxies=%#v want=%#v", got, want)
	}
}

func TestParseReportsInvalidLine(t *testing.T) {
	_, err := Parse("http://proxy.test:8080\nsocks5://proxy.test:1080")
	if err == nil || !strings.Contains(err.Error(), "第 2 行") {
		t.Fatalf("err=%v", err)
	}
}

func TestAccountAffinityAndFailureAreProcessLocal(t *testing.T) {
	values := []string{
		"http://one.test:8080",
		"http://two.test:8080",
		"https://three.test:8443",
	}
	p, err := New(values)
	if err != nil {
		t.Fatal(err)
	}
	initial := p.Candidates("account-1", 2)
	if len(initial) != 2 {
		t.Fatalf("initial=%#v", initial)
	}
	p.MarkFailed("account-1", initial[0].Key)
	fallback := p.Candidates("account-1", 2)
	if len(fallback) != 2 || fallback[0].Key == initial[0].Key {
		t.Fatalf("fallback=%#v initial=%#v", fallback, initial)
	}
	p.MarkSucceeded("account-1", fallback[0].Key)
	if got := p.Candidates("account-1", 1)[0].Key; got != fallback[0].Key {
		t.Fatalf("sticky proxy=%q want=%q", got, fallback[0].Key)
	}

	// Reordering the same set keeps affinity; a real configuration change resets it.
	if err := p.Replace([]string{values[2], values[0], values[1]}); err != nil {
		t.Fatal(err)
	}
	if got := p.Candidates("account-1", 1)[0].Key; got != fallback[0].Key {
		t.Fatalf("reorder reset affinity: got=%q want=%q", got, fallback[0].Key)
	}
	if err := p.Replace([]string{values[0], values[1]}); err != nil {
		t.Fatal(err)
	}
	if got := p.Candidates("account-1", 2); len(got) != 2 {
		t.Fatalf("configuration change did not reset failures: %#v", got)
	}
}

func TestRendezvousHashUsesAllConfiguredProxies(t *testing.T) {
	p, err := New([]string{
		"http://one.test:8080",
		"http://two.test:8080",
		"http://three.test:8080",
	})
	if err != nil {
		t.Fatal(err)
	}
	selected := make(map[string]struct{})
	for i := range 100 {
		candidate := p.Candidates(fmt.Sprintf("account-%d", i), 1)
		selected[candidate[0].Key] = struct{}{}
	}
	if len(selected) != 3 {
		t.Fatalf("selected proxies=%#v", selected)
	}
}
