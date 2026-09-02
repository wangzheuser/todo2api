package openai

import "testing"

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
