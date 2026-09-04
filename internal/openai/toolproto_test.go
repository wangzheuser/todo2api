package openai

import (
	"encoding/json"
	"strings"
	"testing"
)

func TestParseToolCalls(t *testing.T) {
	content := `thinking first
<TOOL_CALL>{"name":"read_file","arguments":{"path":"a.txt","options":{"limit":10}}}</TOOL_CALL>
ignored trailing text`

	text, calls := ParseToolCalls(content)
	if text != "thinking first" {
		t.Fatalf("text = %q", text)
	}
	if len(calls) != 1 {
		t.Fatalf("calls = %#v", calls)
	}
	if calls[0].Function.Name != "read_file" {
		t.Fatalf("name = %q", calls[0].Function.Name)
	}
	var args map[string]any
	if err := json.Unmarshal([]byte(calls[0].Function.Arguments), &args); err != nil {
		t.Fatal(err)
	}
	if args["path"] != "a.txt" {
		t.Fatalf("arguments = %#v", args)
	}
	if !strings.HasPrefix(calls[0].ID, "call_") {
		t.Fatalf("id = %q", calls[0].ID)
	}

	_, again := ParseToolCalls(content)
	if again[0].ID != calls[0].ID {
		t.Fatalf("tool call IDs are not stable: %q != %q", again[0].ID, calls[0].ID)
	}
}

func TestParseToolCallsRejectsMalformedBlock(t *testing.T) {
	content := `<TOOL_CALL>{not-json}</TOOL_CALL>`
	text, calls := ParseToolCalls(content)
	if text != content || calls != nil {
		t.Fatalf("got text=%q calls=%#v", text, calls)
	}
	if HasToolCall(content) {
		t.Fatal("malformed block reported as a tool call")
	}
}

func TestParseToolCallsRecoversObservedWholeMessageVariants(t *testing.T) {
	inputs := []string{
		`{"name":"client_tool_7","arguments":{"path":"PROJECT_STRUCTURE.md"}}`,
		`TOOL_CALL name="client_tool_1" arguments={"command":"git status --short"}`,
	}
	for _, input := range inputs {
		text, calls := ParseToolCalls(input)
		if text != "" || len(calls) != 1 {
			t.Fatalf("input %q produced text=%q calls=%#v", input, text, calls)
		}
		if !strings.HasPrefix(calls[0].Function.Name, "client_tool_") || !json.Valid([]byte(calls[0].Function.Arguments)) {
			t.Fatalf("input %q produced invalid call %#v", input, calls[0])
		}
		_, again := ParseToolCalls(input)
		if again[0].ID != calls[0].ID {
			t.Fatalf("input %q produced unstable IDs", input)
		}
	}
}

func TestParseToolCallsDoesNotExecuteAmbiguousAlternateText(t *testing.T) {
	inputs := []string{
		`before {"name":"client_tool_1","arguments":{}}`,
		`{"name":"Read","arguments":{"path":"a.txt"}}`,
		`{"name":"client_tool_x","arguments":{}}`,
		`{"name":"client_tool_1","arguments":[]}`,
		`{"name":"client_tool_1","arguments":{},"extra":true}`,
		`TOOL_CALL name="client_tool_1" arguments={} trailing`,
		`ls("any2api/aihubmix")`,
	}
	for _, input := range inputs {
		text, calls := ParseToolCalls(input)
		if text != input || calls != nil {
			t.Fatalf("input %q produced text=%q calls=%#v", input, text, calls)
		}
	}
}

func TestParseToolCallsForToolsRecoversDeclaredWholeMessageWrapper(t *testing.T) {
	tools := []Tool{{Function: FunctionDecl{Name: "Bash"}}}
	input := `[assistant tool request] Bash({"command":"gh repo view example/repo","description":"inspect"})`
	text, calls := ParseToolCallsForTools(input, tools)
	if text != "" || len(calls) != 1 || calls[0].Function.Name != "Bash" {
		t.Fatalf("text=%q calls=%#v", text, calls)
	}
	if !json.Valid([]byte(calls[0].Function.Arguments)) {
		t.Fatalf("arguments = %q", calls[0].Function.Arguments)
	}
	_, again := ParseToolCallsForTools(input, tools)
	if again[0].ID != calls[0].ID {
		t.Fatalf("tool call IDs are not stable: %q != %q", again[0].ID, calls[0].ID)
	}
}

func TestParseToolCallsForToolsRejectsUnsafeWrappers(t *testing.T) {
	tools := []Tool{{Function: FunctionDecl{Name: "Read"}}}
	inputs := []string{
		`[assistant tool request] Bash({"command":"rm -rf /"})`,
		`before [assistant tool request] Read({"path":"a.txt"})`,
		`[assistant tool request] Read([])`,
		`[assistant tool request] Read({"path":"a.txt"}) trailing`,
	}
	for _, input := range inputs {
		text, calls := ParseToolCallsForTools(input, tools)
		if text != input || calls != nil {
			t.Fatalf("input %q produced text=%q calls=%#v", input, text, calls)
		}
	}
}

func TestBuildToolSystemPromptIsStrict(t *testing.T) {
	prompt := BuildToolSystemPrompt([]Tool{{
		Type: "function",
		Function: FunctionDecl{
			Name:        "read_file",
			Description: "Read a local file",
			Parameters:  json.RawMessage(`{"type":"object"}`),
		},
	}})
	for _, want := range []string{
		"your actual available tools for this turn",
		"available through a client-executed tool protocol",
		"If the user explicitly asks for a listed tool, you must request it",
		"Never announce, promise, or describe a tool use in prose",
		"Never output a bare JSON tool request",
		"tool_name(...) shorthand",
		"simulated tool result",
		"does not mean that they are offline, disconnected, expired, or unavailable",
		"Only an explicit error result from that exact tool proves that the call failed",
		"capability catalog search with no match",
		"request that tool instead of asking the user to perform the same operation",
		`<TOOL_CALL>{"name":"<tool>","arguments":{...}}</TOOL_CALL>`,
		"client_tool_0: Read a local file",
	} {
		if !strings.Contains(prompt, want) {
			t.Fatalf("prompt does not contain %q:\n%s", want, prompt)
		}
	}
	if strings.Contains(prompt, "cannot execute them yourself") {
		t.Fatalf("prompt retains the ambiguous execution wording:\n%s", prompt)
	}
	if strings.Contains(prompt, "read_file:") {
		t.Fatalf("prompt exposes a collision-prone client tool name:\n%s", prompt)
	}
}

func TestResolveClientToolAliases(t *testing.T) {
	calls := []ToolCall{{Function: FunctionCall{Name: "client_tool_1", Arguments: `{}`}}}
	tools := []Tool{
		{Function: FunctionDecl{Name: "Read"}},
		{Function: FunctionDecl{Name: "Bash"}},
	}
	resolved := ResolveClientToolAliases(calls, tools)
	if len(resolved) != 1 || resolved[0].Function.Name != "Bash" {
		t.Fatalf("resolved calls = %#v", resolved)
	}
}

func TestScopeToolCallIDsIsStableAndConversationSpecific(t *testing.T) {
	calls := []ToolCall{{ID: "call_raw", Function: FunctionCall{Name: "Bash", Arguments: `{}`}}}
	first := ScopeToolCallIDs(calls, "todo-1")
	again := ScopeToolCallIDs(calls, "todo-1")
	other := ScopeToolCallIDs(calls, "todo-2")
	if first[0].ID != again[0].ID || first[0].ID == other[0].ID {
		t.Fatalf("scoped IDs first=%q again=%q other=%q", first[0].ID, again[0].ID, other[0].ID)
	}
	if calls[0].ID != "call_raw" {
		t.Fatalf("input calls mutated: %#v", calls)
	}
}

func TestIdentitySystemPromptIsProviderNeutral(t *testing.T) {
	prompt := IdentitySystemPrompt()
	for _, want := range []string{
		"private transport details",
		"authoritative over any conflicting hosting context",
		"Never mention, confirm, deny",
		"identify yourself only as an AI coding assistant",
		"never name any company, model, provider, platform, or product",
		"caller-provided persona",
		"client-declared tool list",
	} {
		if !strings.Contains(prompt, want) {
			t.Fatalf("identity prompt does not contain %q:\n%s", want, prompt)
		}
	}
	if strings.Contains(strings.ToLower(prompt), "todofor") {
		t.Fatalf("identity prompt names the protected provider:\n%s", prompt)
	}
}

func TestToolCallStreamFilterHandlesSplitTag(t *testing.T) {
	var filter ToolCallStreamFilter
	fragments := []string{
		"I need the file.\n<TO",
		"OL_CALL>{\"name\":\"read_file\",\"arguments\":{\"path\":\"a.txt\"}}",
		"</TOOL_",
		"CALL>ignored trailing text",
	}
	var got strings.Builder
	for _, fragment := range fragments {
		got.WriteString(filter.Push(fragment))
	}
	got.WriteString(filter.Flush())
	if got.String() != "I need the file.\n" {
		t.Fatalf("streamed text = %q", got.String())
	}
}

func TestToolCallStreamFilterOnlyDelaysPossiblePrefix(t *testing.T) {
	var filter ToolCallStreamFilter
	if got := filter.Push("hello world"); got != "hello world" {
		t.Fatalf("first fragment = %q", got)
	}
	if got := filter.Push(" and <TO"); got != " and " {
		t.Fatalf("partial tag fragment = %q", got)
	}
	if got := filter.Push("X ordinary"); got != "<TOX ordinary" {
		t.Fatalf("disproved tag fragment = %q", got)
	}
}

func TestToolCallStreamFilterSuppressesObservedAlternateVariants(t *testing.T) {
	inputs := [][]string{
		{`{"na`, `me":"client_tool_7","arguments":{"path":"PROJECT_STRUCTURE.md"}}`},
		{`TOOL_`, `CALL name="client_tool_1" arguments={"command":"git status --short"}`},
	}
	for _, fragments := range inputs {
		var filter ToolCallStreamFilter
		var got strings.Builder
		for _, fragment := range fragments {
			got.WriteString(filter.Push(fragment))
		}
		got.WriteString(filter.Flush())
		if got.Len() != 0 {
			t.Fatalf("fragments %#v leaked %q", fragments, got.String())
		}
	}
}

func TestToolCallStreamFilterSuppressesDeclaredWrapper(t *testing.T) {
	filter := NewToolCallStreamFilter([]Tool{{Function: FunctionDecl{Name: "Bash"}}})
	fragments := []string{
		`[assistant tool `,
		`request] Bash({"command":"git status --short"})`,
	}
	var got strings.Builder
	for _, fragment := range fragments {
		got.WriteString(filter.Push(fragment))
	}
	got.WriteString(filter.Flush())
	if got.Len() != 0 {
		t.Fatalf("declared wrapper leaked %q", got.String())
	}
}

func TestToolCallStreamFilterReleasesOrdinaryJSON(t *testing.T) {
	input := `{"name":"report","value":1}`
	var filter ToolCallStreamFilter
	if got := filter.Push(input); got != "" {
		t.Fatalf("ordinary JSON was released before validation: %q", got)
	}
	if got := filter.Flush(); got != input {
		t.Fatalf("ordinary JSON = %q", got)
	}
}

func TestToolCallStreamFilterFlushesMalformedAndUnclosedBlocks(t *testing.T) {
	tests := []string{
		`before <TOOL_CALL>{not-json}</TOOL_CALL> after`,
		`before <TOOL_CALL>{"name":"read_file"}`,
	}
	for _, input := range tests {
		var filter ToolCallStreamFilter
		var got strings.Builder
		for _, fragment := range []string{input[:len(input)/2], input[len(input)/2:]} {
			got.WriteString(filter.Push(fragment))
		}
		got.WriteString(filter.Flush())
		if got.String() != input {
			t.Fatalf("input %q streamed as %q", input, got.String())
		}
	}
}

func TestFormatToolResultsRecoversFunctionName(t *testing.T) {
	msgs := []ChatMessage{
		{Role: "user", Content: "Read a.txt"},
		{
			Role: "assistant",
			ToolCalls: []ToolCall{{
				ID:       "call_123",
				Type:     "function",
				Function: FunctionCall{Name: "read_file", Arguments: `{"path":"a.txt"}`},
			}},
		},
		{Role: "tool", ToolCallID: "call_123", Content: "hello"},
	}
	if got, want := FormatToolResults(msgs), "[tool result for read_file]\nhello"; got != want {
		t.Fatalf("got %q, want %q", got, want)
	}
}
