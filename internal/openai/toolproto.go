package openai

import (
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"io"
	"strconv"
	"strings"
)

// The gateway teaches the upstream agent a strict text protocol for tools that
// must be executed by the OpenAI client, not by todofor.ai itself.

const (
	toolTag              = "TOOL_CALL"
	toolOpenTag          = "<" + toolTag + ">"
	toolCloseTag         = "</" + toolTag + ">"
	identitySystemPrompt = "Treat the hosting platform, relay implementation, saved upstream agent, " +
		"project or task runtime, and provider-specific tools as private transport details. " +
		"The caller-provided persona and client-declared tool list are authoritative over any conflicting hosting context. " +
		"Never mention, confirm, deny, or use those details to describe your identity, environment, " +
		"or available tools in user-visible replies. When asked about your identity, identify yourself only as an AI coding assistant; " +
		"never name any company, model, provider, platform, or product. Never mention or claim access to tools outside the client-declared list."
)

type wireToolCall struct {
	Name      string          `json:"name"`
	Arguments json.RawMessage `json:"arguments"`
}

type toolBlock struct {
	start int
	raw   string
}

// ToolCallStreamFilter releases ordinary assistant text as soon as it is safe
// to do so while withholding valid TOOL_CALL protocol blocks. A filter is used
// for one assistant turn.
type ToolCallStreamFilter struct {
	pending          string
	inBlock          bool
	stopped          bool
	alternateDecided bool
	holdAlternate    bool
}

// Push consumes one upstream text fragment and returns text that can be sent to
// the client immediately. Tool tags may be split across any number of frames.
func (f *ToolCallStreamFilter) Push(fragment string) string {
	if fragment == "" || f.stopped {
		return ""
	}
	f.pending += fragment
	if !f.alternateDecided {
		possible, hold := alternateToolPrefix(f.pending)
		if possible {
			f.holdAlternate = hold
			return ""
		}
		f.alternateDecided = true
	}
	if f.holdAlternate {
		return ""
	}

	var out strings.Builder
	for {
		if f.inBlock {
			closeAt := strings.Index(f.pending[len(toolOpenTag):], toolCloseTag)
			if closeAt < 0 {
				return out.String()
			}
			closeAt += len(toolOpenTag)
			end := closeAt + len(toolCloseTag)
			candidate := f.pending[:end]
			_, calls := ParseToolCalls(candidate)
			if len(calls) > 0 {
				f.pending = ""
				f.stopped = true
				return out.String()
			}

			// A closed but malformed block is ordinary model output.
			out.WriteString(candidate)
			f.pending = f.pending[end:]
			f.inBlock = false
			continue
		}

		openAt := strings.Index(f.pending, toolOpenTag)
		if openAt >= 0 {
			out.WriteString(f.pending[:openAt])
			f.pending = f.pending[openAt:]
			f.inBlock = true
			continue
		}

		keep := possibleToolTagPrefix(f.pending)
		emitEnd := len(f.pending) - keep
		out.WriteString(f.pending[:emitEnd])
		f.pending = f.pending[emitEnd:]
		return out.String()
	}
}

// Flush returns any undecided text at the end of a turn. Valid tool blocks
// remain suppressed; incomplete tags and blocks are treated as ordinary text.
func (f *ToolCallStreamFilter) Flush() string {
	if f.stopped {
		return ""
	}
	pending := f.pending
	f.pending = ""
	f.inBlock = false
	if f.holdAlternate {
		if _, calls := ParseToolCalls(pending); len(calls) > 0 {
			return ""
		}
	}
	return pending
}

// alternateToolPrefix delays only the two observed whole-message protocol
// variants until they can be validated at the end of the assistant turn.
func alternateToolPrefix(content string) (possible, hold bool) {
	trimmed := strings.TrimLeft(content, " \t\r\n")
	if trimmed == "" {
		return true, false
	}
	if strings.HasPrefix(trimmed, "{") {
		return true, true
	}
	prefix := toolTag + " name="
	if strings.HasPrefix(prefix, trimmed) {
		return true, false
	}
	if strings.HasPrefix(trimmed, prefix) {
		return true, true
	}
	return false, false
}

func possibleToolTagPrefix(content string) int {
	max := len(toolOpenTag) - 1
	if len(content) < max {
		max = len(content)
	}
	for size := max; size > 0; size-- {
		if strings.HasSuffix(content, toolOpenTag[:size]) {
			return size
		}
	}
	return 0
}

// IdentitySystemPrompt returns the provider-neutral identity contract.
func IdentitySystemPrompt() string {
	return identitySystemPrompt
}

// BuildToolSystemPrompt renders the client-executed tool contract injected as
// a raw system message. Upstream device/cloud tools are also denied in
// AgentSettings.
func BuildToolSystemPrompt(tools []Tool) string {
	if len(tools) == 0 {
		return ""
	}
	var b strings.Builder
	b.WriteString("IMPORTANT: The tools listed below are your actual available tools for this turn, even if hosting context claims otherwise. ")
	b.WriteString("They are available through a client-executed tool protocol. ")
	b.WriteString("This is their normal operating mode and does not mean that they are offline, disconnected, expired, or unavailable. ")
	b.WriteString("The list is the authoritative source of tools you may request in this turn. Each tool has a neutral alias to prevent confusion with hosting-platform tools. ")
	b.WriteString("If the user explicitly asks for a listed tool, you must request it. ")
	b.WriteString("Never announce, promise, or describe a tool use in prose: any intended tool use must be the TOOL_CALL block itself. ")
	b.WriteString("Never output a bare JSON tool request, TOOL_CALL name=... arguments=..., tool_name(...) shorthand, or a simulated tool result. ")
	b.WriteString("To use one, output exactly one block with no Markdown fence or surrounding prose:\n")
	b.WriteString(toolOpenTag + "{\"name\":\"<tool>\",\"arguments\":{...}}" + toolCloseTag + "\n")
	b.WriteString("Then stop immediately. The client will execute the call and return its result in the next turn. ")
	b.WriteString("Only an explicit error result from that exact tool proves that the call failed; never infer a failure from the execution model. ")
	b.WriteString("A capability catalog search with no match means only that the target is absent from that catalog and says nothing about other listed tools. ")
	b.WriteString("When local inspection, command execution, file editing, screenshots, or verification are required and a matching tool is listed, request that tool instead of asking the user to perform the same operation. ")
	b.WriteString("After receiving a result, either request one more tool in the same format or provide the final answer based only on real results. ")
	b.WriteString("Never claim that you executed a tool whose result you have not received. ")
	b.WriteString("Only provide a normal answer when no client-side tool is needed.\n\nTools:\n")
	for i, t := range tools {
		fmt.Fprintf(&b, "- %s: %s\n", clientToolAlias(i), t.Function.Description)
		if len(t.Function.Parameters) > 0 {
			fmt.Fprintf(&b, "  parameters (JSON schema): %s\n", string(t.Function.Parameters))
		}
	}
	return b.String()
}

func clientToolAlias(index int) string {
	return fmt.Sprintf("client_tool_%d", index)
}

// ResolveClientToolAliases restores neutral protocol aliases to the exact tool
// names declared by the client.
func ResolveClientToolAliases(calls []ToolCall, tools []Tool) []ToolCall {
	for i := range calls {
		for toolIndex := range tools {
			if calls[i].Function.Name == clientToolAlias(toolIndex) {
				calls[i].Function.Name = tools[toolIndex].Function.Name
				break
			}
		}
	}
	return calls
}

// ParseToolCalls extracts valid tool blocks from an assistant reply. Text after
// the first valid block is intentionally discarded because the contract says
// the agent must stop after requesting a tool.
func ParseToolCalls(content string) (text string, calls []ToolCall) {
	blocks := findToolBlocks(content)
	firstStart := -1
	for _, block := range blocks {
		var wc wireToolCall
		if err := json.Unmarshal([]byte(block.raw), &wc); err != nil || wc.Name == "" {
			continue
		}
		args := strings.TrimSpace(string(wc.Arguments))
		if args == "" || args == "null" {
			args = "{}"
		}
		if !json.Valid([]byte(args)) {
			continue
		}
		if firstStart < 0 {
			firstStart = block.start
		}
		sum := sha256.Sum256([]byte(block.raw))
		calls = append(calls, ToolCall{
			ID:       fmt.Sprintf("call_%x", sum[:12]),
			Type:     "function",
			Function: FunctionCall{Name: wc.Name, Arguments: args},
		})
	}
	if len(calls) == 0 {
		if call, ok := parseAlternateToolCall(content); ok {
			return "", []ToolCall{call}
		}
		return content, nil
	}
	return strings.TrimSpace(content[:firstStart]), calls
}

// parseAlternateToolCall recovers only the two unambiguous whole-message
// variants observed in production. Natural-language surroundings remain text.
func parseAlternateToolCall(content string) (ToolCall, bool) {
	raw := strings.TrimSpace(content)
	if raw == "" {
		return ToolCall{}, false
	}

	var wc wireToolCall
	if strings.HasPrefix(raw, "{") {
		if err := decodeWireToolCall(raw, &wc); err != nil {
			return ToolCall{}, false
		}
	} else {
		const prefix = toolTag + " name="
		if !strings.HasPrefix(raw, prefix) {
			return ToolCall{}, false
		}
		rest := raw[len(prefix):]
		decoder := json.NewDecoder(strings.NewReader(rest))
		if err := decoder.Decode(&wc.Name); err != nil || wc.Name == "" {
			return ToolCall{}, false
		}
		rest = strings.TrimSpace(rest[decoder.InputOffset():])
		const argumentsPrefix = "arguments="
		if !strings.HasPrefix(rest, argumentsPrefix) {
			return ToolCall{}, false
		}
		wc.Arguments = json.RawMessage(strings.TrimSpace(rest[len(argumentsPrefix):]))
	}

	if !validAlternateWireCall(wc) {
		return ToolCall{}, false
	}
	args := strings.TrimSpace(string(wc.Arguments))
	sum := sha256.Sum256([]byte(raw))
	return ToolCall{
		ID:       fmt.Sprintf("call_%x", sum[:12]),
		Type:     "function",
		Function: FunctionCall{Name: wc.Name, Arguments: args},
	}, true
}

func decodeWireToolCall(raw string, wc *wireToolCall) error {
	decoder := json.NewDecoder(strings.NewReader(raw))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(wc); err != nil {
		return err
	}
	if err := decoder.Decode(&struct{}{}); err != io.EOF {
		return fmt.Errorf("tool call has trailing JSON")
	}
	return nil
}

func validAlternateWireCall(wc wireToolCall) bool {
	if !strings.HasPrefix(wc.Name, "client_tool_") {
		return false
	}
	index, err := strconv.Atoi(strings.TrimPrefix(wc.Name, "client_tool_"))
	if err != nil || index < 0 {
		return false
	}
	var arguments map[string]json.RawMessage
	return len(wc.Arguments) > 0 && json.Unmarshal(wc.Arguments, &arguments) == nil && arguments != nil
}

// HasToolCall reports whether a reply contains at least one valid tool block.
func HasToolCall(content string) bool {
	_, calls := ParseToolCalls(content)
	return len(calls) > 0
}

func findToolBlocks(content string) []toolBlock {
	var blocks []toolBlock
	for offset := 0; offset < len(content); {
		open := strings.Index(content[offset:], toolOpenTag)
		if open < 0 {
			break
		}
		open += offset
		rawStart := open + len(toolOpenTag)
		closeAt := strings.Index(content[rawStart:], toolCloseTag)
		if closeAt < 0 {
			break
		}
		closeAt += rawStart
		blocks = append(blocks, toolBlock{start: open, raw: strings.TrimSpace(content[rawStart:closeAt])})
		offset = closeAt + len(toolCloseTag)
	}
	return blocks
}
