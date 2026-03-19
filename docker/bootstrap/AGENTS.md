## Gmail capability (gws)

You ARE able to read email bodies when the user explicitly asks and OAuth credentials are present.

### Rules

- When asked for “latest emails” or “email body/content”, you MUST use the `exec` tool to run `gws` commands.
- Skills are documentation. Do NOT claim a skill is a callable tool. If you need a skill, use `read_file` to read its `SKILL.md`.
- Do NOT refuse due to “missing tools” for Gmail read operations — `exec` + `gws` is the toolchain.
- Do NOT call tools like `gws_gmail.*`, `gws-gmail`, `gws-gmail-triage`, or any other “skill name” as a tool. Those tools do not exist.
- Keep `exec` commands literal (no `${VAR}`, `$(...)`, or backticks).
- Output: show **From, Subject, Date**, then a **short body preview** (truncate long bodies). Do not print tokens/credentials.

### Latest 3 emails with body (workflow)

IMPORTANT: This is a multi-step workflow. Do not stop after step 1.

1) List latest message IDs (Gmail API):

```bash
gws gmail users messages list --params '{"userId":"me","maxResults":3}'
```

2) For each returned `id`, fetch full message:

```bash
gws gmail users messages get --params '{"userId":"me","id":"MESSAGE_ID","format":"full"}'
```

3) Extract headers + body (prefer `text/plain`), then present to the user.

