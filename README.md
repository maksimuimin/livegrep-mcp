# LiveGrepMCP

A minimal FastMCP server for the LiveGrep search API using the FastMCP protocol as documented at [gofastmcp.com](https://gofastmcp.com/servers/tools).

## Features

- Search code repositories using the LiveGrep API
- Support for regex and text searches
- Path and repo filtering options
- Case sensitivity controls
- Easily integrates with LLM models and other MCP clients
- Available as both a Python tool and a REST resource

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
   If your LiveGrep instance requires authentication, set `LIVEGREP_COOKIE`
   to the cookie header value to send with each request
   (e.g. `session=abc123; other=value`).

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
        "LIVEGREP_API_URL": "https://your.livegrep.com/",
        "LIVEGREP_COOKIE": "session=abc123"
    }
}
```

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

2. **Resource: `livegrep://search-help`**
   - Provides help information about the LiveGrep search syntax

3. **Resource: `livegrep://api-info`**
   - Provides information about the configured LiveGrep API endpoint

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

This will run the LiveGrepMCP server

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
        "livegrep-mcp",
    ],
    "env": {
        "LIVEGREP_API_URL": "https://your.livegrep.com/",
    }
},
```