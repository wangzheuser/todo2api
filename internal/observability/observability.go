package observability

import "context"

type requestIDKey struct{}

// WithRequestID attaches the sanitized request identifier to a request context.
// The value is intentionally kept in a small package so transport and gateway
// layers can correlate one request without depending on each other.
func WithRequestID(ctx context.Context, id string) context.Context {
	return context.WithValue(ctx, requestIDKey{}, id)
}

// RequestID returns the request identifier or a stable placeholder for logs
// emitted by background work and unit tests that do not have an HTTP request.
func RequestID(ctx context.Context) string {
	if ctx == nil {
		return "-"
	}
	if id, ok := ctx.Value(requestIDKey{}).(string); ok && id != "" {
		return id
	}
	return "-"
}
