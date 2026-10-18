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

Merging to `main` deploys the stack through GitHub Actions. The deploy role must be allowed to create an EventBridge Scheduler schedule and to pass the schedule's invoke role to `scheduler.amazonaws.com` (the same kind of IAM access the workflow already uses to create Lambda roles). The workflow passes:

- `EeroSessionSecretName=deviceweave/eero-session`
- `EeroPollSchedule=rate(5 minutes)`
- `EeroAwayMissThreshold=3`

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

Change the schedule or the threshold in `.github/workflows/deploy.yml` (`EeroPollSchedule`, `EeroAwayMissThreshold`) and deploy. Both parameters reject a blank secret name and a blank schedule; an empty override is what broke earlier DeviceWeave deploys. Use a Scheduler expression such as `rate(5 minutes)` or `cron(0/5 * * * ? *)`.

## When the session dies

eero rejects an expired session with HTTP 401 (often `error.session.invalid` or `error.session.refresh`). The poller logs `eero session rejected`, leaves `is_home` as it was, sets `freshness` to `stale`, and sets `stale_reason`. It does the same for a network error, an unreadable secret, or an empty tracked-device table. It will not mark everyone away because the poll failed.

`GET /presence` then still shows the last zone and `is_home`, with `freshness: "stale"`. Run `python scripts/eero_login.py` again. The Lambda cannot refresh the secret itself; its IAM policy is `GetSecretValue` on that one secret.

## Optional network filter

If the eero account admins more than one network, set the template parameter `EeroNetworkId` to the numeric id from the account payload (`/networks/<id>`). Left blank, every network on the account is polled.
