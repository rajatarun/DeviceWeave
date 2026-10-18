# Eero presence

DeviceWeave can tell whether a person is home, and which eero node their phone or other device is connected to, by polling the mesh you already have. No extra hardware.

The poller is a Lambda on an EventBridge Scheduler rate (default every 5 minutes). It writes the same presence item `GET /presence` and the policy engine already read (`pk=home_state` in the presence table). `is_home` stays the occupancy flag. The poller also stores the current node as a coarse zone, the signal, when the device was last seen, and whether that reading is fresh.

**The eero API is unofficial.** It is the cloud API used by the open-source `eero-client` Python library and the Home Assistant eero integration (`POST /login`, `POST /login/verify`, `GET /account`, `GET /networks/{id}/devices` on `api-user.e2ro.com`). eero does not publish it and can change or shut it off without notice. When that happens the poller keeps the last presence, marks it stale, and does not mark anyone away.

## Dedicated admin account

The login CLI cannot use an Amazon eero login. Raja's owner account signs in with Amazon, and this API answers that identifier with HTTP 401 and `error.login.unknown`. Create a separate standard account and invite it as a network admin. The CLI must log in as that dedicated admin, not as the Amazon owner account.

1. Create a new eero account with an email address or a phone number. Choose the one-time code sign-in. Do not choose Amazon login.
2. On the owner's phone, open the eero app and invite that new account as a network admin.
3. Accept the invite on the new account (the email or SMS from eero, or the eero app signed in as the new account).
4. Run the login CLI below and enter that dedicated admin's email or phone, then the one-time code.

If the identifier is still the Amazon account, the CLI stops with a message that repeats these steps. It does not store a session.

## What you run before the first poll

The stack does not create the session secret and it does not know which devices are yours. Do this once, from a checkout of this repo, with AWS credentials that can write Secrets Manager and the tracked-device table in `us-east-1`. The login script is safe to run before or after the stack deploy. A missing secret does not fail the deploy, and it does not mark anyone away.

1. Log in as the dedicated admin from the section above. eero sends a code by SMS or email to that account. The script stores the session and does not print it.

   ```bash
   python scripts/eero_login.py
   ```

   The default secret name is `deviceweave/eero-session`. Override it with `--secret-name` only if the deploy used a different `EeroSessionSecretName`. The name must not be blank. The prompt asks for the dedicated admin email or phone, not the Amazon owner login.

2. Map each device to a person, by MAC or by the name eero shows (nickname, hostname, or display name). The MAC below is a placeholder.

   ```bash
   python scripts/eero_login.py track --person Raja --mac aa:bb:cc:dd:ee:ff
   python scripts/eero_login.py track --person Raja --name "Raja phone"
   ```

   The default table is `deviceweave-eero-tracked-prod` (`--stage` or `--table` to point elsewhere). One person can have several devices. They count as home when any mapped device is connected.

## What happens after merge

Merging to `main` deploys the stack through GitHub Actions. The schedule role has no custom name, so the workflow's `CAPABILITY_IAM` is enough. The deploy role still needs EventBridge Scheduler permissions it has not needed before: `scheduler:CreateSchedule`, `scheduler:GetSchedule`, `scheduler:UpdateSchedule`, and `scheduler:DeleteSchedule`, plus `iam:PassRole` on that role to `scheduler.amazonaws.com`. Creating the role uses the same IAM actions as the Lambda roles. The new tables and Lambdas use the same DynamoDB, Lambda, and logs actions as the rest of the stack. CloudFormation does not create the eero session secret or the device HMAC secret. The workflow passes:

- `EeroSessionSecretName=deviceweave/eero-session`
- `EeroPollSchedule="rate(5 minutes)"` (the quotes are part of the argument SAM parses; without them the space is cut and the value is `rate(5`)
- `EeroAwayMissThreshold=3`
- `EeroDeviceHmacSecretName=deviceweave/eero-device-hmac`

The poller Lambda is not in a VPC. It reads only that secret (`secretsmanager:GetSecretValue`) and writes the presence table. If you have not logged in or registered a device yet, do the steps above. The next poll picks them up. You do not need to redeploy to add a device.

Check the result:

```bash
curl -s "$API_URL/presence"
```

A successful poll looks like:

```json
{
  "is_home": true,
  "updated_at": "2026-10-18T16:05:00+00:00",
  "zone": "Office",
  "node": "Office eero",
  "signal": "strong",
  "room": "unknown",
  "room_confidence": 0,
  "last_seen": "2026-10-18T16:00:00.000Z",
  "freshness": "fresh",
  "source": "eero",
  "people": [
    {
      "person": "Raja",
      "is_home": true,
      "zone": "Office",
      "node": "Office eero",
      "signal": "strong",
      "room": "unknown",
      "room_confidence": 0,
      "last_seen": "2026-10-18T16:00:00.000Z",
      "freshness": "fresh",
      "miss_count": 0
    }
  ]
}
```

`zone` prefers the eero node's location (the room name in the eero app). `node` prefers the node's display name. When several people are home, those aggregate fields are the person seen most recently. The `get_presence` MCP tool returns this same body.

`POST /presence` with `{"is_home": true|false}` still sets the occupancy flag by hand. The next successful poll replaces it from eero.

## Away debounce

A phone that drops off Wi-Fi for one poll does not mark that person away. The poller counts consecutive misses of every device mapped to them. The default threshold is 3, so at a 5-minute schedule someone is marked away after about 15 minutes of absence. The next poll that sees any of their devices marks them home immediately and resets the count. `miss_count` on each person is how many misses are in the current streak.

Change the schedule or the threshold in `.github/workflows/deploy.yml` (`EeroPollSchedule`, `EeroAwayMissThreshold`) and deploy. Both parameters reject a blank secret name and a blank schedule; an empty override is what broke earlier DeviceWeave deploys. Use a Scheduler expression such as `rate(5 minutes)` or `cron(0/5 * * * ? *)`. Keep a space inside SAM's quotes (`EeroPollSchedule="rate(5 minutes)"`). Quotes that only wrap the whole `Key=value` are removed by the shell, and SAM then splits the value on the space.

## When the session dies

eero rejects an expired session with HTTP 401 (often `error.session.invalid` or `error.session.refresh`). The poller logs `eero session rejected`, leaves `is_home` as it was, sets `freshness` to `stale`, and sets `stale_reason`. It does the same for a network error, an unreadable secret, or an empty tracked-device table. It will not mark everyone away because the poll failed.

`GET /presence` then still shows the last zone and `is_home`, with `freshness: "stale"`. Run `python scripts/eero_login.py` again. The Lambda cannot refresh the secret itself; its IAM policy is `GetSecretValue` on the session secret and the device HMAC secret.

## Optional network filter

If the eero account admins more than one network, set the template parameter `EeroNetworkId` to the numeric id from the account payload (`/networks/<id>`). Left blank, every network on the account is polled.

## Room calibration

The node zone is which eero the phone is attached to. A room is a label you teach: stand in the room, take a short run of readings, and the poller later picks the nearest saved room. It will not guess. A tie, an unseen node, or a weak match stays `room: "unknown"` and leaves `zone` as the node location.

### What the cloud API actually reports

The device payload used by `eero-client` and the Home Assistant eero integration carries `connectivity.score_bars` (an integer, meaningful when it is 1–5), `connectivity.signal` and `connectivity.signal_avg` (strings: quality words such as `strong`, `fair`, or `weak`, and sometimes a dBm number), and `connectivity.score` (a float with no published 0–1 scale). The node is `source.location`, or `source.display_name` when location is unset. `last_active` is the last time the client did something, not a signal-sample timestamp.

That list is not a live RSSI feed. Samples taken every 12 seconds during one calibration can be identical, and a fractional `score` is ignored. Each stored sample is the node plus a coarse bucket: `score_bars` when it is 1–5, otherwise the quality word, otherwise a coarse bucket when `signal` is a negative dBm value. There is no invented RSSI field. If two rooms share a node and the bucket does not separate them, classification returns `unknown`.

### HMAC secret

Fingerprints and the presence item store an HMAC-SHA256 of the device MAC, not the MAC. The key is its own secret so a new eero login does not invalidate every room. Create it once in `us-east-1` before the first calibration (the deploy does not create it):

```bash
aws secretsmanager create-secret \
  --name deviceweave/eero-device-hmac \
  --secret-string "$(openssl rand -hex 32)"
```

The name must match `EeroDeviceHmacSecretName`. Replacing the key makes existing fingerprints unmatchable until you calibrate those rooms again. The tracked-device table still holds the MAC you registered with `scripts/eero_login.py track`; that is how the poller finds the phone. Logs from calibration and classification record counts and room labels only.

Occupancy still updates if this secret is missing. Room stays `unknown`.

### Walkthrough

The assistant calls these MCP tools. The same operations are HTTP routes on the calibration Lambda, and they require the MCP bearer token (`Authorization: Bearer …`, the secret `deviceweave/mcp-auth-token`). `GET /presence` stays open and does not return the samples.

| Tool | Arguments | What it does |
| --- | --- | --- |
| `calibrate_room` | `person` (required), `room` (required), `samples` (optional, default 5, max 8) | Queue a sample run. Returns when the run has started, not when it finishes. Calling it again for the same person and room replaces that room's samples. |
| `list_room_fingerprints` | `person` (optional) | List rooms, sample counts, nodes, buckets, and the samples. |
| `delete_room_fingerprint` | `room` (required), `person` (optional) | Delete that room. Omit `person` to delete the label for every person. |

`POST /presence/rooms/calibrate` with `{"person","room","samples"}` is the HTTP form of `calibrate_room` and returns **202**. `GET /presence/rooms` lists. `DELETE /presence/rooms/{room}` deletes. A missing or wrong token is **401**.

Room labels are 1–40 characters: letters, digits, spaces, hyphens, underscores. `unknown` is reserved. A policy that says `room == "Office"` must use that same spelling, including capitals.

1. Create the HMAC secret above, and finish the dedicated-admin login and `track` steps so the person has a device.
2. Stand in the room for about a minute. Ask the assistant to call `calibrate_room` with `person` set to the tracked name (for example `Raja`), `room` set to the label you want in policies (for example `Office`), and `samples` left at 5. The reads are about 12 seconds apart.
3. Wait for that minute. `list_room_fingerprints` should show the room, the node, and a bucket. Repeated identical samples are normal.
4. Walk to the next room and repeat. One room at a time: the phone can only teach the room you are standing in.
5. To redo a room, call `calibrate_room` again with the same `person` and `room`. The new samples replace the old ones. `delete_room_fingerprint` drops a room without replacing it.

The scheduled poller reads the fingerprint table (it cannot write it) and classifies the live node and bucket. Confidence is the nearest bucket distance on that node, reduced when another room on the same node is close. Below 0.5, or when two rooms are equally near, or when the node was never calibrated, `room` is `unknown` and `zone` stays the node. The displayed room changes only after two polls in a row agree, so one odd reading does not flicker it. A Wi-Fi miss during the away debounce holds the current room; it is not stored as a new sample. Live readings are not kept as history. The current presence item holds the room, the confidence, and the two-reading smoothing state.

`GET /presence` and `get_presence` include `room`, `room_confidence`, and `zone`. The policy context field `room` is that label, or `unknown` when presence cannot be read. `room == "Office"` matches only that label. `unknown` does not match it.

The fingerprint table is on-demand, encrypted at rest, and not covered by point-in-time recovery. Only the calibration function can write it, and only the authenticated routes and the MCP tools reach it.

## Room check-ins

A week of labeled check-ins is the training set for a later room model: eero node, coarse signal, time of day, weekday, and the room Raja names. Every row is written to the DeviceWeave check-in table (`deviceweave-room-checkins-<stage>`). Nothing in this path writes a file on the assistant's machine.

The table is pay-per-request, uses the default AWS owned encryption key, and has no TTL, so a week of rows is still there when you train. CloudFormation creates it on the next deploy. There is no new secret and no new deploy parameter. The phone tracked as `iphone` (or whichever person name you passed to `scripts/eero_login.py track`) is the device the server reads.

The reading is the live cloud snapshot at the moment of `record_room_checkin` or `capture_room_reading`. It is not a historical RSSI for a past `asked_at`. When the answer arrives minutes after the question, capture at ask time and label later. Passing `asked_at` to `record_room_checkin` only sets the clock features; the eero fields are still "now".

Hour and weekday come from `asked_at` in `America/Chicago`. Weekday is Monday `0` through Sunday `6`. `asked_at` is the question time. `answered_at` is when the label was stored. Labeling does not change `asked_at`, the hour, the weekday, or the eero fields, and it does not read eero again.

`room` may be `Not home` or any other label of 1–80 characters. It is not limited to the calibration room names. A blank label is rejected.

If eero or the session cannot be read, the row is still stored with `freshness: "stale"` and without `is_home`, so a failed poll is not trained as away. If the poll succeeds and the device is not connected, `freshness` is `fresh` and `is_home` is false. A person who is not in the tracked-device table is rejected before any row is written.

Device ids on a check-in are the same HMAC-SHA256 used for fingerprints (`deviceweave/eero-device-hmac`). The row never stores the MAC or the eero nickname. If the HMAC secret cannot be read, the node and signal are still stored and `device_hmac` is omitted. The calibration function is the only role that can write the table. The poller cannot read or write it. The MCP function invokes the calibration function; it does not write the table itself. The HTTP routes use the same bearer token as `POST /mcp`.

| Tool | Arguments | What it does |
| --- | --- | --- |
| `record_room_checkin` | `person`, `room`, optional `asked_at`, optional `source` | Read eero now, store the label and the snapshot, return the row. |
| `capture_room_reading` | `person`, optional `asked_at`, optional `source` | Store the snapshot with no room. Returns `checkin_id`. |
| `label_room_checkin` | `checkin_id`, `room` | Attach the answer. Does not read eero again. |
| `list_room_checkins` | optional `person`, optional `since` (ISO-8601), optional `limit` (1–200, default 50) | Oldest first. |
| `delete_room_checkin` | `checkin_id` | Delete one row. `deleted` is 0 when the id is already gone. |

`POST /presence/checkins` records. `POST /presence/checkins/capture` snapshots. `POST /presence/checkins/{checkin_id}/label` labels. `GET /presence/checkins` lists. `DELETE /presence/checkins/{checkin_id}` deletes. A missing or wrong token is **401**.

Hourly collection, when the answer is not instant:

1. At the question, call `capture_room_reading` with `person` set to the tracked name (the phone tracked today is `iphone`).
2. When Raja answers, call `label_room_checkin` with that `checkin_id` and the room, or `Not home`.
3. If the answer is already known at ask time, `record_room_checkin` does both in one call.
4. `list_room_checkins` reads the week back out of DynamoDB. `delete_room_checkin` drops a bad row.
