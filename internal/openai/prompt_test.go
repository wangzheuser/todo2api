package openai

import (
	"encoding/json"
	"strings"
	"testing"
)

func TestNormalizeInstructions(t *testing.T) {
	original := []ChatMessage{
		{Role: "system", Content: " system one "},
		{Role: "user", Content: "question"},
		{Role: "developer", Content: "developer one"},
		{Role: "system", Content: "system two"},
		{Role: "assistant", Content: "answer"},
		{Role: "developer", Content: "developer two"},
	}
	req := ChatRequest{System: " top level ", Messages: original}

	got := NormalizeInstructions(req)

	wantSystem := "top level\n\nsystem one\n\nsystem two\n\ndeveloper one\n\ndeveloper two"
	if got.System != wantSystem {
		t.Fatalf("system = %q, want %q", got.System, wantSystem)
	}
	if len(got.Messages) != 2 || got.Messages[0].Role != "user" || got.Messages[1].Role != "assistant" {
		t.Fatalf("messages = %#v", got.Messages)
	}
	if len(original) != 6 || original[0].Content != " system one " || req.System != " top level " {
		t.Fatalf("input request mutated: req=%#v original=%#v", req, original)
	}
}

func TestNormalizeInstructionsWithoutInstructions(t *testing.T) {
	original := []ChatMessage{{Role: "user", Content: "question"}}
	got := NormalizeInstructions(ChatRequest{Messages: original})
	if got.System != "" || len(got.Messages) != 1 || got.Messages[0].Role != "user" || got.Messages[0].Content != "question" {
		t.Fatalf("normalized request = %#v", got)
	}
}

func TestFlattenTurnWithToolsUsesCanonicalNeutralProtocol(t *testing.T) {
	tools := []Tool{
		{Function: FunctionDecl{Name: "Read"}},
		{Function: FunctionDecl{Name: "Bash"}},
	}
	messages := []ChatMessage{
		{Role: "user", Content: "inspect the repository"},
		{Role: "assistant", ToolCalls: []ToolCall{{
			ID: "call-1", Type: "function",
			Function: FunctionCall{Name: "Bash", Arguments: `{"command":"git status --short"}`},
		}}},
		{Role: "tool", ToolCallID: "call-1", Content: "clean"},
	}

	got := FlattenTurnWithTools(messages, tools)
	wantCall := `<TOOL_CALL>{"name":"client_tool_1","arguments":{"command":"git status --short"}}</TOOL_CALL>`
	if !strings.Contains(got, wantCall) || !strings.Contains(got, "[tool result for client_tool_1]\nclean") {
		t.Fatalf("flattened history = %q", got)
	}
	if strings.Contains(got, "[assistant tool request]") || strings.Contains(got, "Bash(") {
		t.Fatalf("flattened history retained conflicting syntax: %q", got)
	}
	var payload wireToolCall
	raw := strings.TrimSuffix(strings.TrimPrefix(wantCall, toolOpenTag), toolCloseTag)
	if err := json.Unmarshal([]byte(raw), &payload); err != nil || payload.Name != "client_tool_1" {
		t.Fatalf("canonical history payload = %#v, %v", payload, err)
	}
}
