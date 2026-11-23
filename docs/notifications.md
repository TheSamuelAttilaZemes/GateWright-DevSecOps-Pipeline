# Notifications (v0.6)

Gatewright can tell you when something needs attention: a failed run, a commit waiting for your
approval, a deployment or a rollback.

```yaml
notify:
  on: [run_failed, approval_needed, deploy_failed, rolled_back]   # default for every channel
  channels:
    - type: ntfy
      url: https://ntfy.sh/your-secret-topic-name
    - type: webhook
      url: https://chat.example.com/hooks/abc123
      secret: WEBHOOK_SIGNING_SECRET
      on: [run_failed, deployed, rolled_back]                     # per-channel override
    - type: email
      smtp: smtp.example.com:587
      from: gatewright@example.com
      to: [you@example.com]
      username_secret: SMTP_USER
      password_secret: SMTP_PASSWORD
    - type: matrix
      homeserver: https://matrix.org
      room: "!AbCdEf:matrix.org"
      token_secret: MATRIX_TOKEN
```

Test every channel with `gatewright notify-test`.

## Events

| Event | Sent when |
|---|---|
| `run_failed` | a run finished with status failed |
| `run_warn` | a run passed with warnings, or only advisory gates failed |
| `run_cancelled` | a run was stopped (Ctrl+C, the watcher shutting down) before it finished |
| `run_skipped` | nothing matched the selection, so nothing ran |
| `run_passed` | a run passed |
| `gate_failed` | a gate failed (one message per gate, in addition to the run message) |
| `approval_needed` | the watcher queued a commit that needs `gatewright approve` |
| `deployed` | a promotion or rollback succeeded |
| `deploy_failed` | a deploy or its verify failed (without automatic rollback) |
| `rolled_back` | verify failed and the previous artifact was redeployed |

Without `on`, channels get `run_failed`, `approval_needed`, `deploy_failed` and `rolled_back`.

## Channels

| Type | Delivers | Settings |
|---|---|---|
| `ntfy` | push notification to phone/desktop via [ntfy](https://ntfy.sh) (or your own ntfy server) | `url` (server + topic), optional `token_secret` (access token) |
| `webhook` | JSON `POST`; Slack, Mattermost, Rocket.Chat and Discord incoming webhooks understand it (`text`/`content` fields) | `url`, optional `secret`: adds `X-Gatewright-Signature: sha256=<HMAC of the body>` |
| `email` | plain-text e-mail via SMTP: STARTTLS on port 587 (`starttls: true`, default), TLS on 465 | `smtp` (`host:port`), `from`, `to`, optional `username_secret`, `password_secret` |
| `matrix` | message in a Matrix room via the client-server API | `homeserver`, `room` (room ID), `token_secret` (access token of a bot user) |

All `*_secret` settings name secrets, looked up like every other secret (`GATEWRIGHT_SECRET_<NAME>`,
`<NAME>`, or `.gatewright/secrets.env`). The values never appear in the config.

The webhook body looks like this:

```json
{"source": "gatewright", "event": "run_failed", "title": "Gatewright: shop FAILED",
 "text": "*Gatewright: shop FAILED*\nPipeline shop: FAILED (main @ f3925bee) ...",
 "content": "...", "run_id": "20261122-085138-a77e", "status": "failed", "commit": "f3925bee..."}
```

## Reliability

- A notification that fails (server down, wrong token, missing secret) **never fails the run or the
  deployment**. It becomes a warning in the run (`gatewright show`) and on the console.
- Each channel has a 10-second timeout.
- Messages include the top three issues and the path of the HTML report.
- Only Python's standard library is used. Nothing extra to install.

## Privacy

Messages contain the pipeline name, commit, gate results and the first lines of error explanations
(which can include file names and code locations). Secrets known to Gatewright are masked everywhere,
but think about who can read the channel. For ntfy.sh, use a long, unguessable topic name or your
own server with access tokens.
