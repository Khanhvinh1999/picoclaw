---
name: gws-gmail
version: 1.0.0
description: "Gmail: Send, read, and manage email."
metadata:
  openclaw:
    category: "productivity"
    requires:
      bins: ["gws"]
    cliHelp: "gws gmail --help"
---

# gmail (v1)

> **PREREQUISITE:** Read `../gws-shared/SKILL.md` for auth, global flags, and security rules. If missing, run `gws generate-skills` to create it.

```bash
gws gmail <resource> <method> [flags]
```

## Telegram-friendly: “latest emails (with body)”

User may ask in natural language, for example:

- "Show me the 3 latest emails and include the body."
- "Get the latest 5 emails from:boss and show the content."

Implementation rule for the agent:

- Use the `exec` tool to run **literal** `gws ...` commands.
- Do **not** use shell interpolation like `${VAR}`, `$(...)`, or backticks.

### Step 1 — List latest message IDs

```bash
gws gmail users messages list --params '{"userId":"me","maxResults":3}'
```

Optional filters:

```bash
gws gmail users messages list --params '{"userId":"me","maxResults":3,"q":"is:unread"}'
gws gmail users messages list --params '{"userId":"me","maxResults":5,"q":"from:boss"}'
```

### Step 2 — Fetch each message (full payload)

For each message id returned above, run:

```bash
gws gmail users messages get --params '{"userId":"me","id":"MESSAGE_ID","format":"full"}'
```

Then extract and present to the user:

- Subject, From, Date
- A short body preview (truncate if long)

## Helper Commands

| Command | Description |
|---------|-------------|
| [`+send`](../gws-gmail-send/SKILL.md) | Send an email |
| [`+triage`](../gws-gmail-triage/SKILL.md) | Show unread inbox summary (sender, subject, date) |
| [`+reply`](../gws-gmail-reply/SKILL.md) | Reply to a message (handles threading automatically) |
| [`+reply-all`](../gws-gmail-reply-all/SKILL.md) | Reply-all to a message (handles threading automatically) |
| [`+forward`](../gws-gmail-forward/SKILL.md) | Forward a message to new recipients |
| [`+read`](../gws-gmail-read/SKILL.md) | Read a message and extract its body or headers |
| [`+watch`](../gws-gmail-watch/SKILL.md) | Watch for new emails and stream them as NDJSON |

## API Resources

### users

  - `getProfile` — Gets the current user's Gmail profile.
  - `stop` — Stop receiving push notifications for the given user mailbox.
  - `watch` — Set up or update a push notification watch on the given user mailbox.
  - `drafts` — Operations on the 'drafts' resource
  - `history` — Operations on the 'history' resource
  - `labels` — Operations on the 'labels' resource
  - `messages` — Operations on the 'messages' resource
  - `settings` — Operations on the 'settings' resource
  - `threads` — Operations on the 'threads' resource

## Discovering Commands

Before calling any API method, inspect it:

```bash
# Browse resources and methods
gws gmail --help

# Inspect a method's required params, types, and defaults
gws schema gmail.<resource>.<method>
```

Use `gws schema` output to build your `--params` and `--json` flags.

