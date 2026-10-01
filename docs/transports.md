# Real tool transports

The native tools call injected seams (`ToolDeps`). Tests pass fakes. The worker,
the job runner and the CLI get `assembly.default_tool_deps`, which enables each
transport only when it is configured.

| Tool | Transport | Enabled by | Notes |
|---|---|---|---|
| `web_search` | `TavilySearch` (`tools/native/tavily.py`) | `DOT_TAVILY_API_KEY` | The key goes in an `Authorization` header and is registered for redaction. Failures return `{"ok": false, "error": "search returned HTTP 401"}`, never the key. |
| `fetch_url` | `HttpxFetcher` (`tools/native/fetch.py`) | always | Public http(s) only. Hostnames are resolved, and every address must be public, so names such as `metadata.google.internal` are refused. Redirects are not followed and bodies are capped at 1 MB. |
| `send_email` | `SmtpTransport` (`tools/native/smtp.py`) | `DOT_SMTP_HOST`, `DOT_SMTP_USERNAME`, `DOT_SMTP_SENDER` (+ `DOT_SMTP_PORT=587`, `DOT_SMTP_STARTTLS=true`) | The password comes from the broker handle `cred:smtp` (default env `DOT_SMTP_CREDENTIAL`, or Secret Manager) for each send. Failures report only the exception type, because server replies can echo the login. |

`send_email` stays behind policy (`approve`), the Guardian and human approval;
configuring SMTP changes none of that.

**Known limit:** a hostname whose DNS answer changes between the check and the
request (DNS rebinding) is not caught. Closing it needs connecting to the
checked address; that belongs with E3.

## Setup

```
DOT_TAVILY_API_KEY=tvly-…
DOT_SMTP_HOST=smtp.gmail.com          # or your relay: SendGrid, Mailgun, SES SMTP
DOT_SMTP_USERNAME=you@example.com
DOT_SMTP_SENDER=Dot <you@example.com>
DOT_SMTP_CREDENTIAL=<app password>    # resolved through cred:smtp
```

## Tests

- `tests/unit/test_transports.py` covers:
  - Tavily request shape, header auth, and failures without the key;
  - SMTP STARTTLS, login with the brokered password, and failures without it;
  - default wiring.
- `tests/unit/test_tools.py` covers fetch refusing private, metadata, mixed
  and unresolvable hosts.
- `tests/live/test_transports_live.py` (`-m live`) runs a real fetch, which
  passes, and a real Tavily search, which needs the key.
