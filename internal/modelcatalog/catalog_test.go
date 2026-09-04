package modelcatalog

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"todo2api/internal/config"
	"todo2api/internal/pool"
)

func TestDefaultCatalogAndPromotionalPricing(t *testing.T) {
	catalog := Default()
	if len(catalog.Models) != 53 {
		t.Fatalf("catalog models = %d", len(catalog.Models))
	}
	if got := catalog.SnapshotAt.Format(time.RFC3339); got != "2026-09-01T00:00:00Z" {
		t.Fatalf("snapshot = %s", got)
	}

	entry := catalog.Models[0]
	pricing := effectivePricing(entry.Pricing, catalog.SnapshotAt, time.Date(2026, 9, 2, 0, 0, 0, 0, time.UTC))
	if pricing.DiscountPercent != 50 || pricing.Current.Input != 5 || pricing.Current.Output != 25 || pricing.Current.Combined != 30 {
		t.Fatalf("pricing = %#v", pricing)
	}
	if pricing.PromotionEndsAt == nil || pricing.PromotionEndsAt.Format(time.RFC3339) != "2026-09-16T00:00:00Z" {
		t.Fatalf("promotion end = %v", pricing.PromotionEndsAt)
	}
}

func TestLoadRejectsDuplicatePublicIDs(t *testing.T) {
	_, err := Load([]byte(`{
        "snapshot_at":"2026-09-01T00:00:00Z",
        "models":[
          {"canonical_id":"one/model","public_id":"model"},
          {"canonical_id":"two/model","public_id":"model"}
        ]
      }`))
	if err == nil {
		t.Fatal("duplicate public ids were accepted")
	}
}

func TestServiceMergesStaticLiveAndAliasModels(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/api/v1/models":
			_ = json.NewEncoder(w).Encode(map[string]any{
				"object": "list",
				"data": []map[string]any{
					{"id": "anthropic/claude-fable-5", "name": "Live Fable"},
					{"id": "new/provider-model", "name": "Live-only model"},
				},
			})
		case "/api/v1/agents":
			_ = json.NewEncoder(w).Encode([]map[string]any{{"id": "agent-1", "model": "template:model"}})
		default:
			http.NotFound(w, r)
		}
	}))
	defer server.Close()

	cfg := &config.Config{
		Upstream: config.UpstreamConfig{BaseURL: server.URL + "/api/v1", PollTimeout: time.Second},
		Pool: config.PoolConfig{Strategy: "round_robin", Keys: []config.AccountKey{{
			APIKey: "key", ProjectID: "project-1",
		}}},
		Models: config.ModelsConfig{
			Default:           "anthropic:anthropic/claude-fable-5",
			Aliases:           map[string]string{"ox-alpha": "anthropic:anthropic/claude-fable-5"},
			FreeAccountModels: []string{"claude-fable-5"},
		},
	}
	p, err := pool.New(cfg)
	if err != nil {
		t.Fatal(err)
	}
	service := NewService(p, cfg.Models.Aliases, cfg.Models.FreeAccountModels)
	// Constructor copies configuration so later mutations cannot alter service behavior.
	cfg.Models.FreeAccountModels[0] = "ox-alpha"
	response := service.Models()
	if response.Total != 54 || response.Available != 3 || !response.AvailabilityComplete {
		t.Fatalf("response totals = %#v", response)
	}
	byID := make(map[string]Model, len(response.Models))
	for _, model := range response.Models {
		byID[model.ID] = model
	}
	if !byID["claude-fable-5"].Available || byID["claude-fable-5"].Name != "Live Fable" {
		t.Fatalf("static live overlay = %#v", byID["claude-fable-5"])
	}
	if !byID["provider-model"].Available || byID["provider-model"].CanonicalID != "new/provider-model" {
		t.Fatalf("live-only model = %#v", byID["provider-model"])
	}
	if !byID["ox-alpha"].Available || byID["ox-alpha"].CanonicalID != "anthropic/claude-fable-5" {
		t.Fatalf("alias model = %#v", byID["ox-alpha"])
	}
	if !byID["claude-fable-5"].FreeAccountCallable {
		t.Fatalf("verified public model = %#v", byID["claude-fable-5"])
	}
	if byID["ox-alpha"].FreeAccountCallable || byID["provider-model"].FreeAccountCallable {
		t.Fatalf("alias or live-only model inherited free status: %#v", byID)
	}
}
