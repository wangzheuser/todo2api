package modelcatalog

import (
	_ "embed"
	"encoding/json"
	"fmt"
	"math"
	"sort"
	"strings"
	"sync"
	"time"

	"todo2api/internal/pool"
	"todo2api/internal/upstream"
)

//go:embed catalog.json
var catalogData []byte

var (
	defaultOnce    sync.Once
	defaultCatalog *Catalog
)

// Catalog is the immutable pricing snapshot embedded in the application.
type Catalog struct {
	SnapshotAt time.Time
	Models     []staticModel
}

type catalogAsset struct {
	SnapshotAt time.Time     `json:"snapshot_at"`
	Models     []staticModel `json:"models"`
}

type staticModel struct {
	CanonicalID         string        `json:"canonical_id"`
	PublicID            string        `json:"public_id"`
	Name                string        `json:"name"`
	Provider            string        `json:"provider"`
	OwnedBy             string        `json:"owned_by"`
	Created             int64         `json:"created"`
	ContextLength       int64         `json:"context_length"`
	MaxCompletionTokens int64         `json:"max_completion_tokens"`
	Pricing             staticPricing `json:"pricing"`
}

type staticPricing struct {
	Base      PricePair        `json:"base"`
	Promotion *staticPromotion `json:"promotion"`
}

type staticPromotion struct {
	DiscountPercent float64   `json:"discount_percent"`
	StartsAt        time.Time `json:"starts_at"`
	EndsAt          time.Time `json:"ends_at"`
}

// PricePair stores input, output, and combined USD prices per million tokens.
type PricePair struct {
	Input    float64 `json:"input"`
	Output   float64 `json:"output"`
	Combined float64 `json:"combined,omitempty"`
}

// Pricing is the effective price returned to the administration UI.
type Pricing struct {
	Currency        string     `json:"currency"`
	Unit            string     `json:"unit"`
	Base            PricePair  `json:"base"`
	Current         PricePair  `json:"current"`
	DiscountPercent float64    `json:"discount_percent"`
	PromotionEndsAt *time.Time `json:"promotion_ends_at,omitempty"`
	SnapshotAt      time.Time  `json:"snapshot_at"`
}

// Model describes one catalog row together with current pool availability.
type Model struct {
	ID                  string   `json:"id"`
	Object              string   `json:"object"`
	Created             int64    `json:"created"`
	OwnedBy             string   `json:"owned_by"`
	Name                string   `json:"name,omitempty"`
	CanonicalID         string   `json:"canonical_id,omitempty"`
	Provider            string   `json:"provider,omitempty"`
	ContextLength       int64    `json:"context_length,omitempty"`
	MaxCompletionTokens int64    `json:"max_completion_tokens,omitempty"`
	Available           bool     `json:"available"`
	AvailabilityReason  string   `json:"availability_reason,omitempty"`
	Pricing             *Pricing `json:"pricing,omitempty"`
}

// Response is the administration model-list payload.
type Response struct {
	Models               []Model   `json:"models"`
	Total                int       `json:"total"`
	Available            int       `json:"available"`
	AvailabilityComplete bool      `json:"availability_complete"`
	PricingUpdatedAt     time.Time `json:"pricing_updated_at"`
}

// Service combines the embedded pricing catalog with the live account pool.
type Service struct {
	catalog *Catalog
	pool    *pool.Pool
	aliases map[string]string
}

// Default returns the validated embedded catalog.
func Default() *Catalog {
	defaultOnce.Do(func() {
		catalog, err := Load(catalogData)
		if err != nil {
			panic(err)
		}
		defaultCatalog = catalog
	})
	return defaultCatalog
}

// Load parses and validates a catalog snapshot.
func Load(data []byte) (*Catalog, error) {
	var asset catalogAsset
	if err := json.Unmarshal(data, &asset); err != nil {
		return nil, fmt.Errorf("decode model catalog: %w", err)
	}
	if asset.SnapshotAt.IsZero() || len(asset.Models) == 0 {
		return nil, fmt.Errorf("model catalog is empty")
	}
	seen := make(map[string]struct{}, len(asset.Models))
	for _, model := range asset.Models {
		if model.CanonicalID == "" || model.PublicID == "" {
			return nil, fmt.Errorf("model catalog contains an empty model id")
		}
		if _, exists := seen[model.PublicID]; exists {
			return nil, fmt.Errorf("model catalog contains duplicate public id %q", model.PublicID)
		}
		seen[model.PublicID] = struct{}{}
	}
	return &Catalog{SnapshotAt: asset.SnapshotAt, Models: asset.Models}, nil
}

// NewService creates a model-list service backed by the current pool.
func NewService(p *pool.Pool, aliases map[string]string) *Service {
	copyAliases := make(map[string]string, len(aliases))
	for alias, target := range aliases {
		copyAliases[alias] = target
	}
	return &Service{catalog: Default(), pool: p, aliases: copyAliases}
}

// Models returns static catalog rows overlaid with live model metadata.
func (s *Service) Models() Response {
	models := make(map[string]Model, len(s.catalog.Models)+len(s.aliases))
	now := time.Now()
	complete := s.pool.ModelCatalogComplete()
	for _, entry := range s.catalog.Models {
		model := staticDescriptor(entry, s.catalog.SnapshotAt, now)
		if live, ok := s.pool.Model(entry.CanonicalID); ok {
			overlayLiveMetadata(&model, live)
			model.Available = true
		} else {
			model.AvailabilityReason = availabilityReason(s.pool.Len(), complete)
		}
		models[model.ID] = model
	}

	for _, live := range s.pool.Models() {
		model, exists := models[live.ID]
		if !exists {
			canonical := live.ID
			if resolved, ok := s.pool.Model(live.ID); ok {
				canonical = resolved.ID
			}
			model = Model{ID: live.ID, Object: "model", CanonicalID: canonical}
		}
		overlayLiveMetadata(&model, live)
		model.Available = true
		model.AvailabilityReason = ""
		models[live.ID] = model
	}

	if s.pool.Len() > 0 {
		for alias, target := range s.aliases {
			live, ok := s.pool.Model(target)
			if !ok {
				continue
			}
			model := descriptorForCanonical(models, live.ID)
			model.ID = alias
			overlayLiveMetadata(&model, live)
			model.Available = true
			model.AvailabilityReason = ""
			models[alias] = model
		}
	}

	response := Response{
		Models:               make([]Model, 0, len(models)),
		AvailabilityComplete: complete,
		PricingUpdatedAt:     s.catalog.SnapshotAt,
	}
	for _, model := range models {
		response.Models = append(response.Models, model)
		if model.Available {
			response.Available++
		}
	}
	sort.Slice(response.Models, func(i, j int) bool { return response.Models[i].ID < response.Models[j].ID })
	response.Total = len(response.Models)
	return response
}

func staticDescriptor(entry staticModel, snapshotAt, now time.Time) Model {
	return Model{
		ID: entry.PublicID, Object: "model", Created: entry.Created, OwnedBy: entry.OwnedBy,
		Name: entry.Name, CanonicalID: entry.CanonicalID, Provider: entry.Provider,
		ContextLength: entry.ContextLength, MaxCompletionTokens: entry.MaxCompletionTokens,
		Pricing: effectivePricing(entry.Pricing, snapshotAt, now),
	}
}

func effectivePricing(pricing staticPricing, snapshotAt, now time.Time) *Pricing {
	discount := 0.0
	var promotionEndsAt *time.Time
	if promotion := pricing.Promotion; promotion != nil && !now.Before(promotion.StartsAt) && now.Before(promotion.EndsAt) {
		discount = promotion.DiscountPercent
		endsAt := promotion.EndsAt
		promotionEndsAt = &endsAt
	}
	factor := 1 - discount/100
	current := PricePair{
		Input:  roundPrice(pricing.Base.Input * factor),
		Output: roundPrice(pricing.Base.Output * factor),
	}
	current.Combined = roundPrice(current.Input + current.Output)
	return &Pricing{
		Currency: "USD", Unit: "per_1m_tokens", Base: pricing.Base, Current: current,
		DiscountPercent: discount, PromotionEndsAt: promotionEndsAt, SnapshotAt: snapshotAt,
	}
}

func overlayLiveMetadata(model *Model, live upstream.ModelInfo) {
	model.Object = "model"
	if live.Created != 0 {
		model.Created = live.Created
	}
	if live.OwnedBy != "" {
		model.OwnedBy = live.OwnedBy
	}
	if live.Name != "" {
		model.Name = live.Name
	}
	if live.ContextLength != 0 {
		model.ContextLength = live.ContextLength
	}
	if live.MaxCompletionTokens != 0 {
		model.MaxCompletionTokens = live.MaxCompletionTokens
	}
	if model.Provider == "" {
		model.Provider = owner(model.CanonicalID, model.OwnedBy)
	}
}

func descriptorForCanonical(models map[string]Model, canonical string) Model {
	for _, model := range models {
		if model.CanonicalID == canonical {
			return model
		}
	}
	return Model{Object: "model", CanonicalID: canonical, Provider: owner(canonical, "")}
}

func availabilityReason(activeAccounts int, complete bool) string {
	if activeAccounts == 0 {
		return "no_active_accounts"
	}
	if !complete {
		return "catalog_incomplete"
	}
	return "not_common_to_pool"
}

func owner(canonical, fallback string) string {
	provider, _, ok := strings.Cut(canonical, "/")
	if ok && provider != "" {
		return provider
	}
	return fallback
}

func roundPrice(value float64) float64 {
	return math.Round(value*1_000_000) / 1_000_000
}
