# LiveGrepMCP

A minimal FastMCP server for the LiveGrep search API using the FastMCP protocol as documented at [gofastmcp.com](https://gofastmcp.com/servers/tools).

## Features

- Search code repositories using the LiveGrep API
- Support for regex and text searches
- Path and repo filtering options
- Case sensitivity controls
- Easily integrates with LLM models and other MCP clients
- Available as both a Python tool and a REST resource
- Signs in through a browser window when the instance needs it (SSO), and keeps the session across restarts

## Setup

1. Create and activate a virtual environment:
   ```bash
   python3 -m venv venv
   source venv/bin/activate  # On macOS/Linux
   ```

2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

3. Configure the LiveGrep API URL:
   ```bash
   cp .env.example .env
   ```
   Edit `.env` and set the `LIVEGREP_API_URL` to your LiveGrep server.
   If your LiveGrep instance requires authentication, there is nothing to
   paste: see [Authentication](#authentication).

## Usage

Run the FastMCP server:

```bash
# Start the FastMCP server
./src/livegrepMCP.py
```

This starts a FastMCP server that an LLM client can connect to for performing code searches.

```bash
# Development
fastmcp dev src/livegrepMCP.py
```

### Running with uv

The script carries inline metadata (PEP 723), so `uv` resolves the dependencies
automatically — no `--with` flags needed:

```bash
uv run src/livegrepMCP.py
```

Example MCP client configuration:

```json
"livegrep": {
    "command": "uv",
    "args": [
        "run",
        "https://raw.githubusercontent.com/mtib/livegrep-mcp/main/src/livegrepMCP.py"
    ],
    "env": {
        "LIVEGREP_API_URL": "https://your.livegrep.com/"
    }
}
```

## Authentication

Instances behind an SSO (like `https://cs.vk.team`, which sends the browser to
`auth.vk.team` and answers the API with `401 {}` without a session) need the
browser's session cookies.

### Browser sign-in (default)

Nothing to paste. When a search finds no session, or the session expired, the
server opens a dedicated Chrome window at `LIVEGREP_API_URL`. Sign in there; the
server then takes the cookies the browser would send to the API (for cs.vk.team:
`jwt_token`, `refresh_token`), checks that the API accepts them, closes the
window and deletes its throwaway profile. Nothing else from the browser is kept.
Its steps are logged to stderr (`livegrep-mcp: sign-in: …`, never values), which
Claude Desktop shows in `~/Library/Logs/Claude/mcp-server-<name>.log`.

The search that triggered it waits up to ~40 s and carries on (Claude Desktop
allows local servers 60 s per call); if signing in takes longer, it returns an
error asking to retry, and the `login` tool waits for the sign-in in progress,
≤45 s per call. Closing the window cancels the sign-in (searches then don't
reopen it for 2 minutes; `login` does). There is one window at a time:
`login force=true` signs in anew even when the session works, and joins a window
that is already open. If the API fails (5xx) while checking the browser's
cookies, the waiting messages say so and the check is retried. Several MCP
processes with the same configuration share one session: when one signs in, the
others pick it up once the API accepts it. Chrome is driven over a pipe, not a
DevTools port, and exits with the server; on macOS and Linux a profile left by a
killed server is removed when the next one starts.

The server keeps the cookies in a private state file
(`~/.livegrep-mcp/session-<scheme>-<host>-<hash>.json`, mode 0600) and follows
any `Set-Cookie` the API answers a successful request with, so restarts resume
the session without a browser. The session lasts as long as the instance lets
it; when the API rejects it, the window opens again.

Chrome is found in its standard location; set `LIVEGREP_CHROME` to use another
Chromium-based browser (Edge, Brave, Yandex, …). Playwright drives it, and the
installed browser is used: no browser download. `LIVEGREP_AUTO_LOGIN=0` stops
searches from opening the window on their own (the `login` tool still does).
Instances that need no sign-in never open a window.

### Pasted cookie (fallback)

On a machine without a browser (the Docker image has none): sign in to LiveGrep
in a private window, copy its cookies for the site from DevTools →
**Application → Cookies** (for cs.vk.team: `jwt_token` and `refresh_token`), set
`LIVEGREP_COOKIE` to `name=value; name2=value2`, close the private window and
restart the MCP client. A new `LIVEGREP_COOKIE` value starts a new session; when
it is rejected, the browser sign-in takes over where there is a browser.

Treat the values as passwords: they are your session. Tool output never echoes
them.

| env var | meaning |
|---|---|
| `LIVEGREP_API_URL` | base URL of the LiveGrep instance; `https://` for the browser sign-in |
| `LIVEGREP_COOKIE` | optional: `name=value; …` from a private-window session instead of the browser sign-in |
| `LIVEGREP_STATE_DIR` | where the session is kept, default `~/.livegrep-mcp` |
| `LIVEGREP_CHROME` | browser for the sign-in window, default: the installed Google Chrome |
| `LIVEGREP_AUTO_LOGIN` | `0` to keep searches from opening the sign-in window by themselves |

## Available Tools and Resources

The LiveGrepMCP server exposes:

1. **Tool: `search_livegrep`**
   - Allows searching through code repositories using the LiveGrep API
   - Parameters:
     - `query` (str): Search string, can include filters like `path:`, `repo:`, etc.
     - `fold_case` (str): Case sensitivity - "auto", "match", or "ignore"
     - `regex` (str): Enable regex search - "true" or "false"
     - `context` (str): Include context in results - "true" or "false"
     - `max_matches` (int, optional): Limit the number of matches returned

2. **Tool: `login`**
   - Signs in through a browser window, or waits for the sign-in in progress
   - Parameters:
     - `wait_s` (int): How long to wait for the user, 5–45 s (default 40)
     - `force` (bool): Sign in again even if the current session works

3. **Resource: `livegrep://search-help`**
   - Provides help information about the LiveGrep search syntax

4. **Resource: `livegrep://api-info`**
   - Provides information about the configured LiveGrep API endpoint and session

## Search Query Syntax

You can use the following modifiers in your search queries:

- Simple text or regex search: `my_function_name`
- Path filter: `path:XYZ` (include only paths containing XYZ)
- Path exclusion: `-path:XYZ` (exclude paths containing XYZ)
- Repository filter: `repo:XYZ` (include only repos containing XYZ)
- Repository exclusion: `-repo:XYZ` (exclude repos containing XYZ)
- Limit results: `max_matches:XYZ` (limit the number of matches returned)

## Docker

Build and run the LiveGrepMCP server using Docker:

```bash
# Build the image
docker build -t livegrep-mcp .

# Run the FastMCP server using Docker
docker run -e LIVEGREP_API_URL=https://your.livegrep.com/ -it livegrep-mcp
```

This will run the LiveGrepMCP server. The image has no browser: for an
instance behind a sign-in, pass `LIVEGREP_COOKIE` (see
[Pasted cookie](#pasted-cookie-fallback)).

Add it to your MCP client like this:

```
"livegrep": {
    "command": "docker",
    "args": [
        "run",
        "-i",
        "--rm",
        "-e",
        "LIVEGREP_API_URL",
        "-e",
        "LIVEGREP_COOKIE",
        "livegrep-mcp",
    ],
    "env": {
        "LIVEGREP_API_URL": "https://your.livegrep.com/",
        "LIVEGREP_COOKIE": "name=value; name2=value2",
    }
},
```

## Tests

End-to-end tests run the server over stdio against a mock LiveGrep behind a mock
SSO, signing in through a headless Chrome:

```bash
uv run --with-requirements requirements.txt --with pytest pytest tests/
```
