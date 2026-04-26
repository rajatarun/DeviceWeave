# DeviceWeave Admin UI — Conversational Agent

## Context

You are working inside an existing Next.js (TypeScript) admin application for
DeviceWeave, an AI-powered IoT automation system. The app already has:

- A tab-based layout (Home, Devices, Providers, Scenes, Learnings, Policies)
- Existing loading, error, and empty-state patterns — follow them exactly
- No new dependencies may be added

This document specifies **only** the changes required. Leave everything else untouched.

---

## API Contract

### POST /execute — conversational mode

Trigger the Bedrock Converse API agent. Include `session_id` to maintain
conversation history; omit it for one-shot execution (existing behaviour).

**Request:**

```json
{
  "session_id": "550e8400-e29b-41d4-a716-446655440000",
  "command": "turn on the living room fan"
}
```

**Response 200:**

```json
{
  "type": "conversational",
  "session_id": "550e8400-e29b-41d4-a716-446655440000",
  "response": "Done! I've turned on the Living Room Fan.",
  "messages_in_session": 2
}
```

**Response 400** — missing or empty command:

```json
{ "error": "Conversational mode requires a non-empty 'command' field." }
```

**Response 502** — Bedrock or device execution failure:

```json
{ "error": "Agent error: <reason>" }
```

---

## TypeScript Types

Add to your shared types file:

```ts
export interface ChatMessage {
  role: "user" | "assistant";
  text: string;
  timestamp: string; // ISO 8601, set client-side at send time
}

export interface ConversationalExecuteRequest {
  session_id: string;
  command: string;
}

export interface ConversationalExecuteResponse {
  type: "conversational";
  session_id: string;
  response: string;
  messages_in_session: number;
}
```

---

## Change 1 — Chat Tab

### 1.1 Tab placement

Add a **Chat** tab to the existing tab bar, placed after the Home tab and
before the Devices tab.

### 1.2 Session ID

- On first render (or after "New conversation"), generate a UUID v4 with
  `crypto.randomUUID()` and store it in component state.
- The same `session_id` is sent with every message in the conversation.
- Do **not** display the raw UUID to the user.

### 1.3 Layout

```
┌──────────────────────────────────────────────────┐
│  Chat                                            │
│                               [New conversation] │
├──────────────────────────────────────────────────┤
│                                                  │
│   (empty state when no messages)                 │
│   "Start a conversation to control your devices" │
│                                                  │
│  ┌─────────────────────────────────────────────┐ │
│  │ User bubble (right-aligned)                 │ │
│  └─────────────────────────────────────────────┘ │
│  ┌─────────────────────────────────────────────┐ │
│  │ Assistant bubble (left-aligned, accent bg)  │ │
│  └─────────────────────────────────────────────┘ │
│                                                  │
├──────────────────────────────────────────────────┤
│  [_______ Type a command _______] [Send]         │
└──────────────────────────────────────────────────┘
```

The message list area is scrollable; always scroll to the bottom when a new
message is added.

### 1.4 Message bubbles

| Property | User bubble | Assistant bubble |
|----------|-------------|-----------------|
| Alignment | Right | Left |
| Background | Primary colour (match existing primary action colour) | Muted/secondary surface |
| Text colour | On-primary | Default body text |
| Border radius | Existing card radius | Existing card radius |
| Max width | 75 % of container | 75 % of container |
| Timestamp | Below bubble, small muted text, `HH:mm` format | Below bubble, small muted text, `HH:mm` format |

### 1.5 Input area

- Single-line text input, placeholder "Type a command…"
- **Send** button to the right; disabled when input is empty or a request is
  in flight
- Pressing `Enter` (without Shift) submits the message
- On submit:
  1. Append the user message to the local message list immediately
  2. Clear the input field
  3. Show a typing indicator (three animated dots) as an assistant placeholder
  4. Call `POST /execute` with `{ session_id, command }`
  5. Replace the typing indicator with the agent's `response` text
  6. On error: remove the typing indicator and show an inline error banner
     below the message list (see 1.6)

### 1.6 Inline error banner

Use the existing alert/banner component, severity: error, placed between the
message list and the input area:

> ⚠ **Could not send message.**
> [error message from response body]

The banner appears on any non-200 response and is dismissed automatically
when the next message is sent successfully.

### 1.7 New conversation

The **[New conversation]** button in the top-right of the tab:

- Generates a new `crypto.randomUUID()` and stores it as the current session ID
- Clears the local message list
- Clears any visible error banner
- Does **not** call any API endpoint
- Is always visible, including during a pending request (it is never disabled)

### 1.8 State matrix

| Condition | UI |
|-----------|-----|
| No messages yet | Empty state: "Start a conversation to control your devices" |
| Request in flight | Typing indicator in assistant position; Send button disabled |
| 200 response | Agent text replaces typing indicator |
| 400 / 502 error | Inline error banner; typing indicator removed |
| Network failure | Inline error banner; typing indicator removed |

---

## Acceptance Criteria

### Chat tab

- [ ] A Chat tab appears immediately after Home in the tab bar
- [ ] On first render the message list is empty with the empty-state copy
- [ ] Typing a command and pressing Send or Enter appends a right-aligned
      user bubble and shows a typing indicator
- [ ] The typing indicator is replaced by the agent's response text in a
      left-aligned bubble
- [ ] Timestamps appear below each bubble in `HH:mm` format
- [ ] The input field clears after each send
- [ ] The Send button is disabled while a request is in flight
- [ ] A failed request shows the inline error banner and removes the
      typing indicator
- [ ] The error banner disappears on the next successful send
- [ ] New conversation clears the message list and error banner without any
      API call
- [ ] The same `session_id` is sent for every message in a session
- [ ] A new `session_id` is generated after New conversation
- [ ] The message list scrolls to the bottom after each new message

### General

- [ ] No hardcoded URLs
- [ ] No new npm packages installed
- [ ] `crypto.randomUUID()` used for session ID generation (no third-party UUID lib)
- [ ] All pre-existing tabs and features work without regression
