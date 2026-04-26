# DeviceWeave Admin UI — Implementation Specification

## Context

You are working inside an existing Next.js (TypeScript) admin application for
DeviceWeave, an AI-powered IoT automation system. The app already has:

- A tab-based layout (Home, Devices, Scenes, Learnings, Policies)
- Existing loading, error, and empty-state patterns — follow them exactly
- No new dependencies may be added

This document specifies **only** the changes required. Leave everything else untouched.

---


### GET /providers

Returns all registered IoT providers.

Response 200:

```json
{
  "providers": [
    {
      "name": "kasa",
      "display_name": "TP-Link Kasa",
      "device_types": ["SmartBulb", "SmartPlug"],
      "configured": true,
      "supports_rename": true
    },
    {
      "name": "govee",
      "display_name": "Govee",
      "device_types": ["GoveeBulb", "GoveePlug"],
      "configured": false,
      "supports_rename": false
    },
    {
      "name": "switchbot",
      "display_name": "SwitchBot",
      "device_types": ["SwitchBotBulb", "SwitchBotFan", "SwitchBotPlug"],
      "configured": false,
      "supports_rename": false
    }
  ],
  "count": 3
}
```

---

### GET /devices

Response 200 — array may be empty when no ingest has run yet:

```json
{ "devices": [], "count": 0 }
```

Response 503 — registry misconfigured:

```json
{ "error": "Device registry not configured (DEVICE_REGISTRY_TABLE env var not set)." }
```

---

### PUT /devices/{id}

Request body — send only the fields being changed:

```json
{ "name": "Living Room Light" }
```

Response 200 — `provider_rename` is present only when `name` was updated:

```json
{
  "device_id": "abc123",
  "updated": ["name"],
  "updated_at": "2026-04-26T10:00:00Z",
  "provider_rename": {
    "kasa": "synced"
  }
}
```

`provider_rename` value meanings:

| Value | Meaning |
|-------|---------|
| `"synced"` | Name pushed to physical device via provider API |
| `"registry_only"` | Provider does not support rename; only registry updated |
| `"failed: <reason>"` | Provider rename attempted but failed; registry still updated |

---

### POST /devices

Response 405 — manual creation is disabled. Remove any UI that calls this endpoint:

```json
{ "error": "Devices are managed via provider ingest. Use POST /ingest to sync." }
```

---

### DELETE /scenes/{scene_id}

Response 200:

```json
{ "scene_id": "work_mode", "status": "deleted" }
```

Response 404:

```json
{ "error": "Scene 'work_mode' not found." }
```

---

### POST /ingest

Trigger a provider sync. Optionally scope to one provider:

```json
{ "provider": "kasa" }
```

---

## TypeScript Types

Add to your shared types file:

```ts
export interface Provider {
  name: string;
  display_name: string;
  device_types: string[];
  configured: boolean;
  supports_rename: boolean;
}

export type ProviderRenameStatus =
  | "synced"
  | "registry_only"
  | `failed: ${string}`;

export interface UpdateDeviceResponse {
  device_id: string;
  updated: string[];
  updated_at: string;
  provider_rename?: Record<string, ProviderRenameStatus>;
}

export interface ProvidersResponse {
  providers: Provider[];
  count: number;
}
```

---

## Change 1 — Devices Tab

### 1.1 Remove manual device creation

- Delete the "Add Device" button from the Devices tab toolbar
- Delete the corresponding dialog, form, and any `handleCreateDevice` logic
- Do not replace it with anything

### 1.2 Edit device — provider rename feedback

After `PUT /devices/{id}` returns 200, check for `provider_rename` in the
response. If present, display a feedback row below the save button — one chip
per provider entry:

| `provider_rename` value | Chip colour | Label |
|-------------------------|-------------|-------|
| `"synced"` | Green | Synced to [Provider Name] |
| `"registry_only"` | Grey | Registry only — [Provider Name] does not support rename |
| `"failed: <reason>"` | Amber | Sync failed — name saved locally |

For amber chips, show `<reason>` in a tooltip on hover. Map provider keys to
display names using the Provider interface. Clear the feedback when the dialog
is closed or a new edit begins.

### 1.3 State matrix

| Condition | UI |
|-----------|-----|
| Loading | Existing skeleton/spinner |
| 200 with devices | Existing device list |
| 200 with empty array | Empty state (see below) |
| 503 | Registry error banner (see Change 4) |
| Any other error | Existing error pattern |

**Empty state:**

> No devices found.
> Run a sync to discover devices from your connected providers.
> **[Sync now]** — calls `POST /ingest` with no body. Show a spinner while
> pending; on success, refetch the device list.

---

## Change 2 — Scenes Tab

### 2.1 Delete action

Add a trash-icon button to each scene card or table row. Position it in the
top-right corner of the card or at the end of the row, consistent with existing
destructive actions in the app.

### 2.2 Confirmation dialog

On click, open a confirmation dialog using the existing dialog/modal component:

- **Title:** Delete "[scene.name]"?
- **Body:** This scene will no longer match commands or natural language
  requests. This cannot be undone from the UI.
- **Actions:** [Cancel] [Delete] — Delete is the primary destructive button

### 2.3 Delete flow

1. On confirm, call `DELETE /scenes/{scene.id}`
2. Immediately remove the scene from local state (optimistic update)
3. On success (200): no further action needed
4. On error: revert the removal and show a toast with the `error` field from
   the response body

### 2.4 Empty state

When the scenes array is empty on load or after all deletions:

> No active scenes.
> All scenes have been removed.

---

## Change 3 — Providers Tab

### 3.1 Tab placement

Add a **Providers** tab to the existing tab bar, placed after the Devices tab.

### 3.2 Data fetching

On mount call `GET /providers`. Apply the existing loading and error patterns.

### 3.3 Provider card

Render one card per provider using the existing card component:

```
┌─────────────────────────────────────────────┐
│  TP-Link Kasa                  ● Connected   │
│                                              │
│  SmartBulb   SmartPlug                       │
│                                              │
│  Renaming devices syncs to the physical      │
│  device.                                     │
│                                              │
│                          [Sync TP-Link Kasa] │
└─────────────────────────────────────────────┘
```

**Status badge:**
- `configured: true` → green badge, label "Connected"
- `configured: false` → grey badge, label "Not configured"

**Device type chips:** render each item in `device_types` as a small grey chip.

**Rename support line:**
- `supports_rename: true` → "Renaming devices syncs to the physical device."
- `supports_rename: false` → "Renaming updates the registry only."

**Sync button:**
- Label: "Sync [display_name]"
- On click: `POST /ingest` with body `{ "provider": provider.name }`
- While pending: show spinner, disable button
- On 200: toast "Sync complete — [display_name]"
- On error: toast "Sync failed — [error message]"

### 3.4 Layout

Responsive CSS grid — 1 column on mobile (below 768 px), 3 columns on
desktop. Gap matches existing card grid spacing.

---

## Change 4 — Registry Error Banner

### 4.1 Trigger

Show this banner on the Devices tab whenever a devices endpoint returns `503`.

### 4.2 Banner

Use the existing alert/banner component at the top of the tab content area,
above everything else:

> ⚠ **Device registry not configured.**
> Set `DEVICE_REGISTRY_TABLE` and redeploy, then run `POST /ingest` to
> populate devices.

Severity: warning (amber/yellow).

### 4.3 Behaviour

- The banner replaces the spinner and empty state — do not show both
- The toolbar and pagination should not render while the banner is visible
- The banner clears automatically on the next successful fetch

---

## Acceptance Criteria

### Devices
- [ ] No "Add Device" button or form exists anywhere in the app
- [ ] Editing a Kasa device name shows a green "Synced to TP-Link Kasa" chip
- [ ] Editing a Govee or SwitchBot device name shows a grey "Registry only" chip
- [ ] An empty registry shows "No devices found" with a working Sync now button
- [ ] A 503 response shows the registry error banner and nothing else

### Scenes
- [ ] Every scene card or row has a trash icon button
- [ ] Clicking it opens a confirmation dialog with the correct scene name
- [ ] Confirming removes the scene from the list immediately
- [ ] A failed delete reverts the removal and shows a toast
- [ ] Empty scenes list shows the empty state copy

### Providers
- [ ] A Providers tab appears after Devices in the tab bar
- [ ] Three provider cards render (Kasa, Govee, SwitchBot)
- [ ] Configured providers show a green Connected badge
- [ ] Each card shows device type chips and the rename support line
- [ ] Sync button shows a spinner while pending and a toast on completion

### General
- [ ] No hardcoded URLs 
- [ ] No new npm packages installed
- [ ] All pre-existing tabs and features work without regression
