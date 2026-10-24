#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "fastmcp>=2.0.0",
#     "httpx>=0.26.0",
#     "playwright>=1.48.0",
#     "python-dotenv>=1.0.0",
# ]
# ///
"""
LiveGrepMCP - A minimal FastMCP client for LiveGrep search API

Sign-in, as hammer-mcp does it: when the API rejects the request (or there is
no session yet), open a dedicated Chrome window at LiveGrep, let the user pass
the SSO there, then take the cookies the browser would send to
LIVEGREP_API_URL and close the window. Nothing else from the browser is kept,
and the throwaway profile is deleted.

The cookies are kept in a private state file and follow Set-Cookie from the
API, so a restart resumes the session without a browser, and processes with
the same configuration share it.
"""
import asyncio
import atexit
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any, AsyncIterator, Awaitable, Callable, Dict, Literal, Optional
from dotenv import load_dotenv
import httpx
import urllib.parse
from pydantic import Field

from fastmcp import FastMCP

# Load environment variables from .env file
load_dotenv()

LIVEGREP_API_URL = os.getenv("LIVEGREP_API_URL")
if not LIVEGREP_API_URL:
    print("Error: LIVEGREP_API_URL environment variable not set")
    sys.exit(1)

LIVEGREP_API_URL = LIVEGREP_API_URL.rstrip("/")
SEARCH_ENDPOINT = f"{LIVEGREP_API_URL}/api/v2/search"

# Optional cookie for authenticated LiveGrep instances, instead of the browser sign-in
LIVEGREP_COOKIE = os.getenv("LIVEGREP_COOKIE")

AUTO_LOGIN = os.getenv("LIVEGREP_AUTO_LOGIN") != "0"
LOGIN_TIMEOUT_S = 10 * 60
POLL_S = 0.5
# After the user closes the window, don't pop it up again on every tool call.
RETRY_AFTER_CANCEL_S = 2 * 60
# Claude Desktop gives local servers 60 s per call, and a timed-out call gets
# the server restarted (window and all). Stay well inside; the sign-in itself
# keeps going between calls.
CALL_BUDGET_S = 50
RERUN_RESERVE_S = 10

NO_BROWSER = (
    "Without a browser on this machine: sign in to LiveGrep in a private window elsewhere, copy its cookies for "
    'this site from DevTools → Application → Cookies, set LIVEGREP_COOKIE to "name=value; name2=value2", close '
    "that window and restart the MCP."
)
SIGN_IN = "Call the login tool: it opens a Chrome window where the user signs in. " + NO_BROWSER


@asynccontextmanager
async def _lifespan(server: FastMCP) -> AsyncIterator[Dict[str, Any]]:
    _remove_stale_profiles()
    try:
        yield {}
    finally:
        # Before the event loop goes: its shutdown cancels Playwright's own
        # tasks too, and a sign-in could then neither close its window nor
        # remove its profile.
        await _stop_sign_in()


# Initialize FastMCP server
mcp = FastMCP(name="LiveGrepMCP 🔍", lifespan=_lifespan)


def _note(msg: str) -> None:
    """Progress for the MCP client's log (stderr); never cookie values."""
    sys.stderr.write(f"livegrep-mcp: sign-in: {msg}\n")


def _config_problem() -> Optional[str]:
    u = urllib.parse.urlsplit(LIVEGREP_API_URL)
    if u.scheme == "https" or (u.scheme == "http" and u.hostname in ("127.0.0.1", "localhost", "::1")):
        return None
    return "LIVEGREP_API_URL must use https:// for the browser sign-in (the session cookies must not travel in clear text)"


class LivegrepError(Exception):
    def __init__(self, message: str, auth: bool = False):
        super().__init__(message)
        # The session is missing or rejected: a browser sign-in fixes it.
        self.auth = auth


# ---------------------------------------------------------------- session state

# Printable ASCII: anything else would break (or smuggle into) the Cookie header.
_COOKIE_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")
_COOKIE_VALUE = re.compile(r"[\x20-\x3a\x3c-\x7e]*")


def _valid(cookies: Dict[Any, Any]) -> Dict[str, str]:
    return {
        k: v
        for k, v in cookies.items()
        if isinstance(k, str) and isinstance(v, str) and _COOKIE_NAME.fullmatch(k) and _COOKIE_VALUE.fullmatch(v)
    }


_jar: Dict[str, str] = _valid(
    {name.strip(): value.strip() for name, eq, value in (p.partition("=") for p in (LIVEGREP_COOKIE or "").split(";")) if eq}
)

# The state belongs to the configured URL and cookie: pasting a new cookie
# starts over. Processes configured alike share it (and its updates).
_SEED = hashlib.sha256((LIVEGREP_COOKIE or "").encode()).hexdigest()
STATE_FILE = Path(os.getenv("LIVEGREP_STATE_DIR") or "~/.livegrep-mcp").expanduser() / (
    "session-" + re.sub(r"[^\w.-]", "_", LIVEGREP_API_URL.replace("://", "-", 1)) + f"-{_SEED[:12]}.json"
)


def _load_state() -> bool:
    """Adopt the saved session if it differs from the jar; True when it did."""
    try:
        state = json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        return False  # no state yet
    if not isinstance(state, dict) or state.get("seed") != _SEED or not isinstance(state.get("cookies"), dict):
        return False
    saved = _valid(state["cookies"])
    if saved == _jar:
        return False
    _jar.clear()
    _jar.update(saved)
    return True


def _save_state() -> None:
    try:
        STATE_FILE.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=STATE_FILE.parent, prefix=f"{STATE_FILE.name}.")  # 0600, always a new file
        with os.fdopen(fd, "w") as f:
            json.dump({"seed": _SEED, "updated": datetime.now(timezone.utc).isoformat(), "cookies": _jar}, f)
        os.replace(tmp, STATE_FILE)
    except OSError as e:
        sys.stderr.write(f"livegrep-mcp: cannot save session state to {STATE_FILE}: {e}\n")


_load_state()


def _cookie_header(cookies: Dict[str, str]) -> Dict[str, str]:
    return {"Cookie": "; ".join(f"{k}={v}" for k, v in cookies.items())} if cookies else {}


def _absorb_set_cookie(response: httpx.Response, jar: Dict[str, str]) -> None:
    """The server may refresh the session via Set-Cookie: keep up with it."""
    for sc in response.headers.get_list("set-cookie"):
        name, eq, value = sc.split(";", 1)[0].partition("=")
        name, value = name.strip(), value.strip()
        if not eq or not _valid({name: value}):
            continue
        expired = re.search(r";\s*max-age=(0|-\d+)", sc, re.I) or re.search(r";\s*expires=[^;]*\b19[0-7]\d\b", sc, re.I)
        if expired or value == "":
            jar.pop(name, None)
        else:
            jar[name] = value


def _rejected(response: httpx.Response) -> bool:
    """The auth gate turned the request away: 401, or a redirect to (or a page of) the sign-in."""
    if response.status_code == 401 or response.is_redirect:
        return True
    return response.is_success and "text/html" in response.headers.get("content-type", "")


# ---------------------------------------------------------------- requests


async def _send(payload: Dict[str, str], cookies: Dict[str, str]) -> httpx.Response:
    headers = {"Content-Type": "application/x-www-form-urlencoded", **_cookie_header(cookies)}
    try:
        async with httpx.AsyncClient() as client:
            return await client.post(SEARCH_ENDPOINT, content=urllib.parse.urlencode(payload), headers=headers)
    except httpx.HTTPError as e:
        raise LivegrepError(f"Error making request: {e}") from None


async def _post(payload: Dict[str, str], retried: bool = False) -> Any:
    sent = dict(_jar)
    response = await _send(payload, sent)
    if _rejected(response):
        # Its Set-Cookie (a cleared or CSRF cookie) is no session: neither kept
        # nor saved over one another process holds. The session may have
        # changed while the request was in flight, there or here.
        if not retried and (_load_state() or _jar != sent):
            return await _post(payload, True)
        raise LivegrepError(f"{LIVEGREP_API_URL} rejected the request (HTTP {response.status_code}): not signed in. {SIGN_IN}", auth=True)
    before = dict(_jar)
    _absorb_set_cookie(response, _jar)
    if _jar != before:
        _save_state()
    try:
        response.raise_for_status()
        return response.json()
    except httpx.HTTPStatusError as e:
        raise LivegrepError(f"Error making request: {e}") from None
    except ValueError:
        raise LivegrepError(f"Expected JSON from {SEARCH_ENDPOINT}, got: {response.text[:300]}") from None


# ---------------------------------------------------------------- browser sign-in

_login_task: Optional["asyncio.Task[None]"] = None
_cancelled_at = float("-inf")
_probe_problem: Optional[str] = None  # why the API could not check the browser's cookies last time

# A tiny search that only has to get past the auth gate.
_PROBE = {"q": "livegrep-mcp max_matches:1", "fold_case": "auto", "regex": "false", "context": "false"}


def _start_login(auto: bool, force: bool = False) -> "asyncio.Task[None]":
    """
    Start a sign-in, or join the one in progress (one window at a time).
    `auto` is a tool call that found no session: it does not reopen a window
    the user just closed. `force` does not take a session another process saves.
    """
    global _login_task
    problem = _config_problem()  # never send SSO cookies over plain http
    if problem:
        raise LivegrepError(problem)
    if _login_task and not _login_task.done():
        return _login_task
    if auto and time.monotonic() - _cancelled_at < RETRY_AFTER_CANCEL_S:
        raise LivegrepError("The LiveGrep sign-in window was closed. Call the login tool to open it again.", auth=True)
    _login_task = asyncio.ensure_future(_sign_in(force))
    _login_task.add_done_callback(_retrieve)  # callers may have stopped waiting
    return _login_task


def _retrieve(f: "asyncio.Future[Any]") -> None:
    if not f.cancelled():
        f.exception()


async def _await_login(auto: bool, force: bool, wait_s: float) -> bool:
    """Start (or join) the browser sign-in and wait for it a while; True when it finished."""
    task = _start_login(auto, force)
    done, _ = await asyncio.wait({task}, timeout=max(wait_s, 0))
    if not done:
        return False
    task.result()  # its error, if it failed
    return True


def _waiting() -> str:
    checking = f" The browser has cookies, but checking them failed ({_probe_problem}); retrying." if _probe_problem else ""
    return f"A Chrome window is open at {LIVEGREP_API_URL}: the user should sign in there.{checking}"


async def _with_login(fn: Callable[[], Awaitable[Any]]) -> Any:
    """
    Run a request; when the session is missing or expired, open the browser
    sign-in, wait for it, and run the request once more.
    """
    deadline = time.monotonic() + CALL_BUDGET_S
    try:
        return await fn()
    except LivegrepError as e:
        if not (AUTO_LOGIN and e.auth):
            raise
    if not await _await_login(True, False, deadline - time.monotonic() - RERUN_RESERVE_S):
        raise LivegrepError(
            f"Not signed in to LiveGrep yet. {_waiting()} Ask the user to sign in, then call this tool again (the "
            "login tool waits for it).",
            auth=True,
        )
    return await fn()


async def _stop_sign_in() -> None:
    """Close the sign-in window and remove its profile."""
    if _login_task and not _login_task.done():
        _login_task.cancel()
        await asyncio.wait({_login_task}, timeout=15)


_PROFILE = re.compile(r"livegrep-mcp-login-(\d+)-.*")


def _remove_stale_profiles() -> None:
    """Profiles of sign-ins whose server was killed: Chrome exits with the server, its profile stays."""
    if os.name == "nt":
        return  # os.kill(pid, 0) is no liveness check there
    for d in Path(tempfile.gettempdir()).glob("livegrep-mcp-login-*"):
        m = _PROFILE.fullmatch(d.name)
        try:
            st = d.lstat()
            if not m or not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
                continue  # only our own profile dirs: /tmp may be shared
            os.kill(int(m.group(1)), 0)
        except ProcessLookupError:
            shutil.rmtree(d, ignore_errors=True)
        except (OSError, OverflowError):
            pass  # alive, or not a pid


async def _probe(cookies: Dict[str, str]) -> Optional[bool]:
    """Whether the API takes these cookies; None when it could not tell (5xx, unreachable)."""
    global _probe_problem
    try:
        response = await _send(_PROBE, cookies)
        problem = f"the API answers HTTP {response.status_code}" if response.status_code >= 500 else None
    except LivegrepError:
        problem = f"cannot reach {SEARCH_ENDPOINT}"
    if problem and problem != _probe_problem:
        _note(f"checking the browser's cookies: {problem}; retrying")
    _probe_problem = problem
    if problem:
        return None
    if _rejected(response):
        return False
    _absorb_set_cookie(response, cookies)
    return True


async def _sign_in(force: bool) -> None:
    global _cancelled_at, _probe_problem
    try:
        from playwright.async_api import Error as PlaywrightError, async_playwright
    except ImportError:
        raise LivegrepError(f"The browser sign-in needs the playwright package (pip install playwright). {NO_BROWSER}") from None

    _probe_problem = None
    profile = tempfile.mkdtemp(prefix=f"livegrep-mcp-login-{os.getpid()}-")
    # The profile holds a live SSO session: remove it even when the server exits mid-sign-in.
    remove_profile = lambda: shutil.rmtree(profile, ignore_errors=True)  # noqa: E731
    atexit.register(remove_profile)
    playwright = context = None
    try:
        playwright = await async_playwright().start()
        chrome = os.getenv("LIVEGREP_CHROME")
        try:
            context = await playwright.chromium.launch_persistent_context(
                profile,
                **({"executable_path": chrome} if chrome else {"channel": "chrome"}),
                headless=os.getenv("LIVEGREP_LOGIN_HEADLESS") == "1",  # tests only
                chromium_sandbox=True,
                no_viewport=True,
                args=["--window-size=1000,800"],
            )
        except PlaywrightError as e:
            raise LivegrepError(
                f"Cannot start Chrome for the LiveGrep sign-in ({str(e).strip().splitlines()[0]}). Install Google "
                f"Chrome, or set LIVEGREP_CHROME to the executable of a Chromium-based browser. {NO_BROWSER}"
            ) from None
        _note(f"opened a browser window at {LIVEGREP_API_URL}")
        closed = False

        def on_close(_: Any) -> None:
            nonlocal closed
            closed = True

        context.on("close", on_close)
        page = context.pages[0] if context.pages else await context.new_page()
        asyncio.ensure_future(page.bring_to_front()).add_done_callback(_retrieve)
        asyncio.ensure_future(page.goto(f"{LIVEGREP_API_URL}/")).add_done_callback(_retrieve)

        deadline = time.monotonic() + LOGIN_TIMEOUT_S
        cookies: Dict[str, str] = {}
        probed: Optional[Dict[str, str]] = None
        while True:
            try:
                # Only what the browser would send to the API: nothing else from it is kept.
                cookies = _valid({c["name"]: c["value"] for c in await context.cookies(SEARCH_ENDPOINT)})
            except PlaywrightError:
                closed = True
            # On macOS Chrome outlives its last window, so a closed window shows up as no tabs.
            closed = closed or not context.pages
            # The cookies changed (also just before the window closed): see if the API takes them now.
            failing = False
            if cookies and cookies != probed:
                verdict = await _probe(cookies)
                if verdict:
                    _jar.clear()
                    _jar.update(cookies)
                    _save_state()
                    _cancelled_at = float("-inf")
                    _note("signed in; session saved")
                    return
                failing = verdict is None  # check the same cookies again in a moment
                if not failing:
                    probed = cookies
            if closed:
                _cancelled_at = time.monotonic()
                _note("the window was closed")
                raise LivegrepError(
                    "The LiveGrep sign-in window was closed before signing in finished. Call the login tool to try again.",
                    auth=True,
                )
            if time.monotonic() > deadline:
                raise LivegrepError("Signing in to LiveGrep took longer than 10 minutes; call the login tool to try again.", auth=True)
            # Another MCP process sharing the session may have signed in meanwhile.
            if not force and _load_state() and await _probe(dict(_jar)):
                _note("another process signed in; using its session")
                return
            await asyncio.sleep(2 if failing else POLL_S)
    finally:
        try:
            if context is not None:
                try:
                    await context.close()
                except Exception:
                    pass
            if playwright is not None:
                try:
                    await playwright.stop()
                except Exception:
                    pass
        finally:
            remove_profile()
            atexit.unregister(remove_profile)


# ---------------------------------------------------------------- tools


@mcp.tool()
async def search_livegrep(
    query: str,
    fold_case: Literal["auto", "match", "ignore"] = "auto",
    regex: Literal["true", "false"] = "true",
    context: Literal["true", "false"] = "true",
    max_matches: Optional[int] = None
) -> Dict[str, Any]:
    """
    Search through LiveGrep API

    When LiveGrep needs a sign-in, a browser window opens for the user; on a
    "Not signed in yet" error, call the login tool to wait for it.

    Parameters:
    - query (str): Search query - can include modifiers like:
        - path:XYZ (include only paths containing XYZ)
        - -path:XYZ (exclude paths containing XYZ)
        - repo:XYZ (include only repos containing XYZ)
        - -repo:XYZ (exclude repos containing XYZ)
    - fold_case (str): Case sensitivity ("auto", "match", or "ignore")
    - regex (str): Enable regex search ("true" or "false")
    - context (str): Include context in results ("true" or "false")
    - max_matches (int, optional): Limit the number of matches returned

    Returns:
    - dict: API response with search results
    """
    # If max_matches is provided, add it to the query string
    if max_matches is not None:
        query = f"{query} max_matches:{max_matches}"

    # Create the payload
    payload = {
        "q": query,
        "fold_case": fold_case,
        "regex": regex,
        "context": context
    }

    try:
        return await _with_login(lambda: _post(payload))
    except LivegrepError as e:
        return {"error": str(e), "status": "error"}


@mcp.tool()
async def login(
    wait_s: Annotated[int, Field(ge=5, le=45, description="How long to wait for the user to finish signing in")] = 40,
    force: Annotated[bool, Field(description="Sign in again even if the current session works")] = False,
) -> Dict[str, Any]:
    """
    Sign in to LiveGrep through a browser: opens a Chrome window where the user
    signs in; the MCP then keeps that session across restarts. search_livegrep
    opens the window by itself when the session is missing or expired, so call
    this to wait for a sign-in in progress, or with force=true to sign in anew
    although the current session works. Returns waiting:true after at most
    wait_s (≤45 s): keep calling login until signed_in is true.
    """
    try:
        if not force and not (_login_task and not _login_task.done()):  # else keep waiting for that sign-in
            try:
                await _post(_PROBE)
                return {"signed_in": True, "already": True}
            except LivegrepError as e:
                if not e.auth:
                    raise
        if not await _await_login(False, force, wait_s):
            return {
                "signed_in": False,
                "waiting": True,
                "message": f"{_waiting()} Call login again to keep waiting.",
            }
        return {"signed_in": True, "session_state": str(STATE_FILE)}
    except LivegrepError as e:
        return {"error": str(e), "status": "error"}


@mcp.resource("livegrep://search-help")
def get_search_help() -> Dict[str, str]:
    """
    Get help information for LiveGrep search syntax
    """
    return {
        "description": "LiveGrep Search Syntax Help",
        "basic_search": "Simple text or regex search",
        "path_filter": "path:XYZ (include only paths containing XYZ)",
        "path_exclude": "-path:XYZ (exclude paths containing XYZ)",
        "repo_filter": "repo:XYZ (include only repos containing XYZ)",
        "repo_exclude": "-repo:XYZ (exclude repos containing XYZ)",
        "max_matches": "max_matches:XYZ (limit the number of matches returned)",
        "fold_case_options": ["auto", "match", "ignore"],
        "regex_options": ["true", "false"],
        "context_options": ["true", "false"]
    }


@mcp.resource("livegrep://api-info")
def get_api_info() -> Dict[str, Any]:
    """
    Get information about the configured LiveGrep API
    """
    return {
        "api_url": LIVEGREP_API_URL,
        "endpoint": SEARCH_ENDPOINT,
        "method": "POST",
        "content_type": "application/x-www-form-urlencoded",
        "cookie_auth": bool(_jar),
        "cookies_sent": sorted(_jar),
        "session_state": str(STATE_FILE),
    }


if __name__ == "__main__":
    mcp.run()
