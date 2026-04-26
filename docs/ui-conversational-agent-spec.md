# DeviceWeave Admin UI — Conversational Agent

## Context

You are working inside an existing Next.js (TypeScript) admin application for
DeviceWeave, an AI-powered IoT automation system. The app already has:

- A tab-based layout (Home, Devices, Providers, Scenes, Learnings, Policies)
- A Home tab with a text input box that sends one-shot commands to `POST /execute`
- Existing loading, error, and empty-state patterns — follow them exactly
- No new dependencies may be added

This document specifies **only** the changes required. Leave everything else untouched.

**Do not add a new tab.** The change is entirely inside the existing **Home** tab.

---

## API Contract

### POST /execute — conversational mode

The same endpoint the Home tab already calls. Adding `session_id` to the body
activates conversational mode — the backend keeps the full message history for
that session.

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

## Change 1 — Replace the Home Tab Command Box

### 1.1 What to remove

Remove the current Home tab command input and its one-shot response display
entirely. Replace the whole area with the conversational interface described
below. The tab bar and tab label ("Home") do not change.

### 1.2 Session ID

- On first render of the Home tab, generate a UUID v4 with
  `crypto.randomUUID()` and store it in component state.
- The same `session_id` is sent with every message until the user starts a
  new conversation.
- Do **not** display the raw UUID to the user anywhere.

### 1.3 Layout

The Home tab content area becomes:

```
┌──────────────────────────────────────────────────┐
│  Home                            [New conversation]│
├──────────────────────────────────────────────────┤
│                                                  │
│  (empty state when no messages)                  │
│  "Ask me anything — I can control your devices   │
│   and run scenes."                               │
│                                                  │
│  ┌─────────────────────────────────────────────┐ │
│  │                  User bubble (right-aligned) │ │
│  └─────────────────────────────────────────────┘ │
│  ┌─────────────────────────────────────────────┐ │
│  │ Assistant bubble (left-aligned, accent bg)  │ │
│  └─────────────────────────────────────────────┘ │
│                                                  │
├──────────────────────────────────────────────────┤
│  [_______ Type a command… _________________][Send]│
└──────────────────────────────────────────────────┘
```

The message list area is scrollable and fills all available vertical space
between the header and the input bar. Always scroll to the bottom when a new
message arrives.

### 1.4 Message bubbles

| Property | User bubble | Assistant bubble |
|----------|-------------|-----------------|
| Alignment | Right | Left |
| Background | Primary colour (match existing primary action colour) | Muted/secondary surface |
| Text colour | On-primary | Default body text |
| Border radius | Existing card radius | Existing card radius |
| Max width | 75 % of container | 75 % of container |
| Timestamp | Below bubble, small muted text, `HH:mm` format | Below bubble, small muted text, `HH:mm` format |

### 1.5 Input bar

- Single-line text input spanning the full available width minus the Send button
- Placeholder text: "Type a command…"
- **Send** button to the right; disabled when the input is empty or a request
  is in flight
- Pressing `Enter` (without `Shift`) submits the message
- On submit:
  1. Append a user bubble to the message list immediately (optimistic)
  2. Clear the input field
  3. Show a typing indicator (three animated dots) as an assistant placeholder
  4. Call `POST /execute` with `{ session_id, command }`
  5. Replace the typing indicator with the assistant bubble containing
     `response` from the API
  6. On any non-200 response or network failure: remove the typing indicator
     and show the inline error banner (see 1.6)

### 1.6 Inline error banner

Use the existing alert/banner component (severity: error) pinned between the
message list and the input bar:

> ⚠ **Could not send message.**
> [error field from the response body, or "Network error" on fetch failure]

The banner appears only when the last send failed. It is dismissed
automatically when the next message sends successfully.

### 1.7 New conversation button

Place a **[New conversation]** button in the top-right of the Home tab header,
aligned with the tab title. On click:

- Call `crypto.randomUUID()` and store it as the new `session_id`
- Clear the local message list
- Clear any visible error banner
- Do **not** call any API endpoint
- The button is always enabled, including while a request is in flight

### 1.8 State matrix

| Condition | UI |
|-----------|-----|
| No messages yet | Empty state copy centred in the message area |
| Request in flight | Typing indicator in assistant position; Send disabled |
| 200 response | Typing indicator replaced by assistant bubble |
| 400 / 502 error | Inline error banner; typing indicator removed |
| Network failure | Inline error banner; typing indicator removed |

---

## Acceptance Criteria

### Home tab

- [ ] The existing one-shot command input and response area are gone
- [ ] The Home tab now shows a scrollable message list above a sticky input bar
- [ ] An empty-state message is shown when no messages exist in the session
- [ ] Typing a command and pressing Send or Enter appends a right-aligned user
      bubble immediately and shows a typing indicator
- [ ] The typing indicator is replaced by a left-aligned assistant bubble
      containing the agent's response
- [ ] Timestamps appear below each bubble in `HH:mm` format
- [ ] The input field clears after each send
- [ ] The Send button is disabled while a request is in flight
- [ ] A failed request shows the inline error banner and removes the typing indicator
- [ ] The error banner disappears on the next successful send
- [ ] The **[New conversation]** button is visible in the top-right of the tab
- [ ] Clicking it clears messages and errors and generates a new session ID
      without calling any endpoint
- [ ] Every message in a session shares the same `session_id`
- [ ] A new `session_id` is generated after New conversation
- [ ] The message list scrolls to the bottom after each new message

### General

- [ ] No new tab is added to the tab bar
- [ ] No hardcoded URLs
- [ ] No new npm packages installed
- [ ] `crypto.randomUUID()` is used for session ID generation (no third-party UUID lib)
- [ ] All other tabs and features work without regression
