package openai

import (
	"encoding/json"
	"fmt"
	"strings"
)

// NormalizeInstructions promotes system and developer messages into the
// request-level system prompt without mutating the caller's message slice.
func NormalizeInstructions(req ChatRequest) ChatRequest {
	system := make([]string, 0, 1)
	developer := make([]string, 0, 1)
	messages := make([]ChatMessage, 0, len(req.Messages))
	if instruction := strings.TrimSpace(req.System); instruction != "" {
		system = append(system, instruction)
	}
	for _, message := range req.Messages {
		instruction := strings.TrimSpace(message.Content)
		switch message.Role {
		case "system":
			if instruction != "" {
				system = append(system, instruction)
			}
		case "developer":
			if instruction != "" {
				developer = append(developer, instruction)
			}
		default:
			messages = append(messages, message)
		}
	}
	req.System = strings.Join(append(system, developer...), "\n\n")
	req.Messages = messages
	return req
}

// FlattenTurn renders the messages that must be sent to the upstream for the
// current turn. tool-result messages are formatted so the agent can read them.
func FlattenTurn(msgs []ChatMessage) string {
	return FlattenTurnWithTools(msgs, nil)
}

// FlattenTurnWithTools renders prior tool calls with the same canonical,
// neutral protocol taught to the upstream model for the current request.
func FlattenTurnWithTools(msgs []ChatMessage, tools []Tool) string {
	var b strings.Builder
	toolNames := toolNamesByID(msgs)
	for i, m := range msgs {
		switch m.Role {
		case "system":
			b.WriteString("[system] " + m.Content)
		case "assistant":
			if len(m.ToolCalls) > 0 {
				for callIndex, tc := range m.ToolCalls {
					if callIndex > 0 {
						b.WriteByte('\n')
					}
					b.WriteString(canonicalHistoryToolCall(tc, tools))
				}
			} else {
				b.WriteString("[assistant] " + m.Content)
			}
		case "tool":
			name := historyToolName(toolResultName(m, toolNames), tools)
			fmt.Fprintf(&b, "[tool result for %s]\n%s", name, m.Content)
		default:
			b.WriteString(m.Content)
		}
		if i < len(msgs)-1 {
			b.WriteString("\n\n")
		}
	}
	return b.String()
}

// LastToolResults returns the trailing tool-result messages (a follow-up turn).
func LastToolResults(msgs []ChatMessage) []ChatMessage {
	start := len(msgs)
	for start > 0 && msgs[start-1].Role == "tool" {
		start--
	}
	return msgs[start:]
}

// FormatToolResults renders tool-result messages into a single follow-up body.
// Pass the complete OpenAI history so tool names can be recovered from the
// preceding assistant tool_calls when tool result messages omit name.
func FormatToolResults(msgs []ChatMessage) string {
	return FormatToolResultsWithTools(msgs, nil)
}

// FormatToolResultsWithTools uses the same neutral aliases as the canonical
// tool protocol so follow-up results cannot reinforce client-specific syntax.
func FormatToolResultsWithTools(msgs []ChatMessage, tools []Tool) string {
	results := LastToolResults(msgs)
	if len(results) == 0 {
		results = msgs
	}
	toolNames := toolNamesByID(msgs)
	var b strings.Builder
	for i, m := range results {
		name := historyToolName(toolResultName(m, toolNames), tools)
		fmt.Fprintf(&b, "[tool result for %s]\n%s", name, m.Content)
		if i < len(results)-1 {
			b.WriteString("\n\n")
		}
	}
	return b.String()
}

func canonicalHistoryToolCall(call ToolCall, tools []Tool) string {
	arguments := strings.TrimSpace(call.Function.Arguments)
	if arguments == "" || arguments == "null" {
		arguments = "{}"
	}
	if !json.Valid([]byte(arguments)) {
		arguments = "{}"
	}
	payload, _ := json.Marshal(wireToolCall{
		Name: historyToolName(call.Function.Name, tools), Arguments: json.RawMessage(arguments),
	})
	return toolOpenTag + string(payload) + toolCloseTag
}

func historyToolName(name string, tools []Tool) string {
	for i, tool := range tools {
		if tool.Function.Name == name || name == clientToolAlias(i) {
			return clientToolAlias(i)
		}
	}
	return name
}

func toolNamesByID(msgs []ChatMessage) map[string]string {
	names := make(map[string]string)
	for _, msg := range msgs {
		for _, call := range msg.ToolCalls {
			if call.ID != "" {
				names[call.ID] = call.Function.Name
			}
		}
	}
	return names
}

func toolResultName(msg ChatMessage, names map[string]string) string {
	if msg.Name != "" {
		return msg.Name
	}
	if name := names[msg.ToolCallID]; name != "" {
		return name
	}
	if msg.ToolCallID != "" {
		return msg.ToolCallID
	}
	return "unknown tool"
}
