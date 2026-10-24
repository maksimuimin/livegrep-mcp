"""
End-to-end: the MCP server over stdio against a mock LiveGrep behind a mock
SSO, signing in through a headless Chrome.

    uv run --with-requirements requirements.txt --with pytest pytest tests/
"""
import asyncio
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

import pytest
from fastmcp import Client
from fastmcp.client.transports import StdioTransport

SERVER = str(Path(__file__).resolve().parent.parent / "src" / "livegrepMCP.py")
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
pytestmark = pytest.mark.skipif(
    not (os.getenv("LIVEGREP_CHROME") or os.path.exists(CHROME) or shutil.which("google-chrome")), reason="no Chrome installed"
)


class Mock:
    """LiveGrep whose API answers 401 {} without a valid jwt_token, as cs.vk.team does."""

    def __init__(self):
        self.valid = set()
        self.minted = 0
        self.sso_ready = True
        self.ready_browsers = None  # or the IdP sessions (one per browser) that may finish signing in
        self.browsers = 0
        self.rotate = False
        self.public = False
        self.gate = "401"  # how the API turns away a request without a session: 401, 302 or html
        self.api_down = False
        self.failures = 0
        self.junk_cookie = False
        self.searches = []
        mock = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def cookie(self, name):
                for part in (self.headers.get("Cookie") or "").split(";"):
                    k, _, v = part.strip().partition("=")
                    if k == name:
                        return v
                return None

            def reply(self, status, body=b"", headers=()):
                self.send_response(status)
                for k, v in headers:
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path in ("/", "/search/"):
                    if self.cookie("jwt_token") in mock.valid:
                        return self.reply(200, b"<title>code search</title>", [("Content-Type", "text/html")])
                    return self.reply(302, headers=[("Location", "/sso")])
                if self.path == "/sso":  # the identity provider: the user signs in here
                    browser = self.cookie("idp")
                    if browser is None:
                        mock.browsers += 1
                        browser = str(mock.browsers)
                    ready = mock.sso_ready if mock.ready_browsers is None else int(browser) in mock.ready_browsers
                    if ready:
                        return self.reply(302, headers=[("Location", "/callback?code=c")])
                    page = b'<meta http-equiv="refresh" content="0.3"><title>sign in</title>'
                    return self.reply(200, page, [("Content-Type", "text/html"), ("Set-Cookie", f"idp={browser}; Path=/sso")])
                if self.path.startswith("/callback"):
                    mock.minted += 1
                    token = f"jwt{mock.minted}"
                    mock.valid.add(token)
                    return self.reply(302, headers=[
                        ("Set-Cookie", f"jwt_token={token}; Path=/; HttpOnly"),
                        ("Set-Cookie", f"refresh_token=refresh{mock.minted}; Path=/; HttpOnly"),
                        ("Set-Cookie", "elsewhere=1; Path=/other"),
                        ("Set-Cookie", "pref=caf\u00e9; Path=/".encode().decode("latin-1")),  # UTF-8 on the wire
                        ("Location", "/search/"),
                    ])
                return self.reply(404)

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode()
                if mock.api_down:
                    mock.failures += 1
                    return self.reply(502, b"<h1>502 Bad Gateway</h1>", [("Content-Type", "text/html")])
                token = self.cookie("jwt_token")
                if not mock.public and token not in mock.valid:
                    # Gates set cookies on a rejection too: they are no session.
                    if mock.gate == "302":
                        mock.minted_csrf = getattr(mock, "minted_csrf", 0) + 1
                        return self.reply(302, headers=[("Location", "/sso"), ("Set-Cookie", f"_csrf=c{mock.minted_csrf}; Path=/")])
                    if mock.gate == "html":
                        return self.reply(200, b"<title>sign in</title>", [("Content-Type", "text/html; charset=utf-8")])
                    return self.reply(401, b"{}", [("Content-Type", "application/json"), ("Set-Cookie", "jwt_token=; Max-Age=0; Path=/")])
                headers = [("Content-Type", "application/json")]
                if mock.junk_cookie:
                    headers.append(("Set-Cookie", "pref=caf\u00e9; Path=/"))
                q = parse_qs(body)["q"][0]
                if not q.startswith("livegrep-mcp "):
                    mock.searches.append((q, self.headers.get("Cookie")))
                if mock.rotate and token:
                    mock.rotate = False
                    mock.valid.discard(token)
                    mock.minted += 1
                    mock.valid.add(f"jwt{mock.minted}")
                    headers.append(("Set-Cookie", f"jwt_token=jwt{mock.minted}; Path=/; HttpOnly"))
                result = {"results": [{"tree": "repo", "path": "src/a.c", "line": q}]}
                return self.reply(200, json.dumps(result).encode(), headers)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


@pytest.fixture
def env(tmp_path):
    mock = Mock()
    (tmp_path / "tmp").mkdir()
    base = {
        "PATH": os.environ["PATH"],
        "HOME": os.environ["HOME"],
        "TMPDIR": str(tmp_path / "tmp"),
        "LIVEGREP_API_URL": mock.url,
        "LIVEGREP_STATE_DIR": str(tmp_path / "state"),
        "LIVEGREP_LOGIN_HEADLESS": "1",
    }
    if os.getenv("LIVEGREP_CHROME"):
        base["LIVEGREP_CHROME"] = os.environ["LIVEGREP_CHROME"]
    yield mock, base, tmp_path
    mock.server.shutdown()


def session(base, tmp_path, **extra):
    """An MCP client session with a fresh server process."""
    log = open(tmp_path / "server.log", "a")
    transport = StdioTransport(command=sys.executable, args=[SERVER], env={**base, **extra}, log_file=log)
    return Client(transport)


async def call(client, tool, **args):
    r = await client.call_tool(tool, args, timeout=120)
    return json.loads(r.content[0].text)


def run(coro):
    return asyncio.run(coro)


def state_files(tmp_path):
    return sorted((tmp_path / "state").glob("session-*.json"))


def test_signs_in_through_the_browser_and_resumes_after_restart(env):
    mock, base, tmp_path = env

    async def first():
        async with session(base, tmp_path) as c:
            assert sorted(t.name for t in await c.list_tools()) == ["login", "search_livegrep"]
            r = await call(c, "search_livegrep", query="foo")
            assert r["results"][0]["line"] == "foo", r
            assert "jwt1" not in json.dumps(r)
            assert await call(c, "login") == {"signed_in": True, "already": True}

    run(first())
    assert mock.minted == 1
    assert mock.searches == [("foo", "jwt_token=jwt1; refresh_token=refresh1")]  # not the Path=/other cookie
    [state] = state_files(tmp_path)
    assert state.name.startswith("session-http-127.0.0.1_")
    assert stat.S_IMODE(state.stat().st_mode) == 0o600
    assert stat.S_IMODE(state.parent.stat().st_mode) == 0o700
    assert json.loads(state.read_text())["cookies"] == {"jwt_token": "jwt1", "refresh_token": "refresh1"}
    assert list((tmp_path / "tmp").glob("livegrep-mcp-login-*")) == []  # the throwaway profile is gone
    log = (tmp_path / "server.log").read_text()
    assert "livegrep-mcp: sign-in: signed in; session saved" in log
    assert "jwt1" not in log

    async def restarted():  # no browser this time
        async with session(base, tmp_path, LIVEGREP_CHROME="/nonexistent") as c:
            assert (await call(c, "search_livegrep", query="bar"))["results"]

    run(restarted())
    assert mock.minted == 1


def test_follows_set_cookie_across_restarts(env):
    mock, base, tmp_path = env
    mock.valid.add("jwt0")

    async def rotate():
        async with session(base, tmp_path, LIVEGREP_COOKIE="jwt_token=jwt0", LIVEGREP_CHROME="/nonexistent") as c:
            mock.rotate = mock.junk_cookie = True  # a non-ASCII value would break the Cookie header
            assert (await call(c, "search_livegrep", query="one"))["results"]
            mock.junk_cookie = False
            assert (await call(c, "search_livegrep", query="two"))["results"]

    run(rotate())
    assert [cookie for _, cookie in mock.searches] == ["jwt_token=jwt0", "jwt_token=jwt1"]
    [state] = state_files(tmp_path)
    assert json.loads(state.read_text())["cookies"] == {"jwt_token": "jwt1"}

    async def restarted():  # the pasted cookie is long dead: the saved one is used
        async with session(base, tmp_path, LIVEGREP_COOKIE="jwt_token=jwt0", LIVEGREP_CHROME="/nonexistent") as c:
            assert (await call(c, "search_livegrep", query="three"))["results"]

    run(restarted())
    assert mock.searches[-1] == ("three", "jwt_token=jwt1")


def test_expired_session_without_auto_login(env):
    mock, base, tmp_path = env
    mock.valid.add("jwt0")
    extra = {"LIVEGREP_COOKIE": "jwt_token=jwt0", "LIVEGREP_AUTO_LOGIN": "0"}

    async def scenario():
        async with session(base, tmp_path, **extra) as c:
            assert (await call(c, "search_livegrep", query="one"))["results"]
            mock.valid.clear()  # the session expired
            r = await call(c, "search_livegrep", query="two")
            assert r["status"] == "error" and "login tool" in r["error"] and "HTTP 401" in r["error"], r
            assert (await call(c, "login"))["signed_in"] is True
            assert (await call(c, "search_livegrep", query="three"))["results"]

    run(scenario())
    assert mock.minted == 1
    assert mock.searches[-1] == ("three", "jwt_token=jwt1; refresh_token=refresh1")


@pytest.mark.parametrize("gate", ["401", "302", "html"])
def test_recognizes_the_auth_gate(env, gate):
    mock, base, tmp_path = env
    mock.gate = gate

    async def scenario():
        async with session(base, tmp_path) as c:
            assert (await call(c, "search_livegrep", query="x"))["results"]

    run(scenario())
    assert mock.minted == 1
    assert mock.searches == [("x", "jwt_token=jwt1; refresh_token=refresh1")]


def test_login_waits_for_the_user_and_can_be_forced(env):
    mock, base, tmp_path = env
    mock.sso_ready = False  # the user has not signed in yet

    async def scenario():
        async with session(base, tmp_path) as c:
            r = await call(c, "login", wait_s=5)
            assert r["signed_in"] is False and r["waiting"] is True, r
            r = await call(c, "login", wait_s=5, force=True)  # joins the open window: one at a time
            assert r["signed_in"] is False and r["waiting"] is True, r
            assert mock.browsers == 1
            threading.Timer(2, lambda: setattr(mock, "sso_ready", True)).start()
            r = await call(c, "search_livegrep", query="x")  # so does a search
            assert r.get("results"), r
            assert await call(c, "login") == {"signed_in": True, "already": True}
            assert (await call(c, "login", force=True))["signed_in"] is True  # signs in again regardless

    run(scenario())
    assert mock.minted == 2
    assert list((tmp_path / "tmp").glob("livegrep-mcp-login-*")) == []


def test_another_process_signs_in_meanwhile(env):
    mock, base, tmp_path = env
    mock.ready_browsers = set()

    async def scenario():
        async with session(base, tmp_path) as waiting, session(base, tmp_path) as other:
            assert (await call(waiting, "login", wait_s=5))["waiting"] is True
            still_waiting = asyncio.ensure_future(call(waiting, "login", wait_s=30))
            await asyncio.sleep(1)
            mock.ready_browsers = {2}  # only the other process's window gets through
            assert (await call(other, "login", wait_s=30))["signed_in"] is True
            r = await still_waiting  # takes the other's session and closes its window
            assert r["signed_in"] is True and "already" not in r, r
            assert not chrome_running(tmp_path)
            assert (await call(waiting, "search_livegrep", query="x"))["results"]

    run(scenario())
    assert mock.minted == 1
    assert mock.searches == [("x", "jwt_token=jwt1; refresh_token=refresh1")]
    assert list((tmp_path / "tmp").glob("livegrep-mcp-login-*")) == []


def test_a_rejected_request_elsewhere_does_not_end_a_sign_in(env):
    mock, base, tmp_path = env
    mock.gate = "302"
    mock.sso_ready = False

    async def scenario():
        async with session(base, tmp_path) as signing_in, session(base, tmp_path, LIVEGREP_AUTO_LOGIN="0") as other:
            assert (await call(signing_in, "login", wait_s=5))["waiting"] is True
            r = await call(other, "search_livegrep", query="x")  # 302 + Set-Cookie: _csrf=…
            assert r["status"] == "error", r
            assert state_files(tmp_path) == []
            r = await call(signing_in, "login", wait_s=5)
            assert r["waiting"] is True and chrome_running(tmp_path), r
            mock.sso_ready = True
            assert (await call(signing_in, "login", wait_s=30))["signed_in"] is True
            assert (await call(other, "search_livegrep", query="y"))["results"]

    run(scenario())
    assert mock.minted == 1


def test_a_session_saved_elsewhere_is_taken_only_once_it_works(env):
    mock, base, tmp_path = env
    mock.sso_ready = False

    def save(cookies):  # what another process with this configuration would write
        (tmp_path / "state").mkdir(exist_ok=True)
        seed = hashlib.sha256(b"").hexdigest()
        name = "session-" + re.sub(r"[^\w.-]", "_", mock.url.replace("://", "-", 1)) + f"-{seed[:12]}.json"
        (tmp_path / "state" / name).write_text(json.dumps({"seed": seed, "cookies": cookies}))

    async def scenario():
        async with session(base, tmp_path) as c:
            assert (await call(c, "login", wait_s=5))["waiting"] is True
            save({"jwt_token": "expired"})
            r = await call(c, "login", wait_s=5)
            assert r["waiting"] is True and chrome_running(tmp_path), r
            mock.valid.add("jwt0")
            save({"jwt_token": "jwt0"})
            r = await call(c, "login", wait_s=30)
            assert r["signed_in"] is True and not chrome_running(tmp_path), r
            assert (await call(c, "search_livegrep", query="x"))["results"]

    run(scenario())
    assert mock.minted == 0
    assert mock.searches == [("x", "jwt_token=jwt0")]


def test_picks_up_a_session_another_process_refreshed(env):
    mock, base, tmp_path = env
    mock.valid.add("jwt0")
    extra = {"LIVEGREP_COOKIE": "jwt_token=jwt0", "LIVEGREP_CHROME": "/nonexistent"}

    async def scenario():
        async with session(base, tmp_path, **extra) as a, session(base, tmp_path, **extra) as b:
            assert (await call(a, "search_livegrep", query="one"))["results"]
            mock.rotate = True
            assert (await call(b, "search_livegrep", query="two"))["results"]  # jwt0 is retired here
            r = await call(a, "search_livegrep", query="three")  # 401 with jwt0, then the saved jwt1
            assert r.get("results"), r

    run(scenario())
    assert mock.searches == [("one", "jwt_token=jwt0"), ("two", "jwt_token=jwt0"), ("three", "jwt_token=jwt1")]


def chrome_running(tmp_path):
    return subprocess.run(["pgrep", "-f", str(tmp_path / "tmp" / "livegrep-mcp-login-")], capture_output=True).returncode == 0


def wait_until(check, timeout=15):
    deadline = time.monotonic() + timeout
    while not check():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.2)


def test_closing_the_window_cancels_the_sign_in(env):
    mock, base, tmp_path = env
    mock.sso_ready = False

    async def scenario():
        async with session(base, tmp_path) as c:
            assert (await call(c, "login", wait_s=5))["waiting"] is True
            subprocess.run(["pkill", "-f", str(tmp_path / "tmp" / "livegrep-mcp-login-")])
            wait_until(lambda: not chrome_running(tmp_path))
            await asyncio.sleep(1)
            r = await call(c, "search_livegrep", query="x")  # does not reopen the window right away
            assert r["status"] == "error" and "window was closed" in r["error"], r
            mock.sso_ready = True
            assert (await call(c, "login", wait_s=30))["signed_in"] is True  # login does
            assert (await call(c, "search_livegrep", query="y"))["results"]

    run(scenario())
    assert mock.minted == 1


def test_a_failing_api_is_reported_and_a_window_closed_after_signing_in_keeps_the_session(env):
    mock, base, tmp_path = env
    mock.sso_ready = False

    async def scenario():
        async with session(base, tmp_path) as c:
            assert (await call(c, "login", wait_s=5))["waiting"] is True
            mock.api_down = mock.sso_ready = True  # the user signs in while the backend is down
            r = await call(c, "login", wait_s=5)
            assert r["waiting"] is True and "HTTP 502" in r["message"], r
            # The user signed in, saw the LiveGrep page, and closed the window
            # before a check got through: right after a failed one, the next is 2 s away.
            seen = mock.failures
            wait_until(lambda: mock.failures > seen)
            subprocess.run(["pkill", "-f", str(tmp_path / "tmp" / "livegrep-mcp-login-")])
            mock.api_down = False
            wait_until(lambda: not chrome_running(tmp_path))
            r = await call(c, "login", wait_s=30)
            assert r["signed_in"] is True, r
            assert (await call(c, "search_livegrep", query="x"))["results"]

    run(scenario())
    assert mock.minted == 1
    assert "window was closed" not in (tmp_path / "server.log").read_text()
    assert "checking the browser's cookies: the API answers HTTP 502; retrying" in (tmp_path / "server.log").read_text()


def test_exiting_mid_sign_in_closes_the_window(env):
    mock, base, tmp_path = env
    mock.sso_ready = False

    async def scenario():
        async with session(base, tmp_path) as c:
            assert (await call(c, "login", wait_s=5))["waiting"] is True
            assert chrome_running(tmp_path)

    run(scenario())
    wait_until(lambda: not chrome_running(tmp_path))
    wait_until(lambda: not list((tmp_path / "tmp").glob("livegrep-mcp-login-*")))


def test_without_a_browser_explains_the_cookie_fallback(env):
    mock, base, tmp_path = env

    async def scenario():
        async with session(base, tmp_path, LIVEGREP_CHROME="/nonexistent") as c:
            r = await call(c, "search_livegrep", query="x")
            assert r["status"] == "error" and "LIVEGREP_CHROME" in r["error"] and "LIVEGREP_COOKIE" in r["error"], r

    run(scenario())
    assert list((tmp_path / "tmp").glob("livegrep-mcp-login-*")) == []


def test_public_instance_needs_no_sign_in(env):
    mock, base, tmp_path = env
    mock.public = True

    async def scenario():
        async with session(base, tmp_path, LIVEGREP_CHROME="/nonexistent") as c:
            assert (await call(c, "search_livegrep", query="x"))["results"]
            assert await call(c, "login") == {"signed_in": True, "already": True}

    run(scenario())
    assert mock.searches == [("x", None)]
    assert state_files(tmp_path) == []


def test_removes_profiles_left_by_a_killed_server(env):
    mock, base, tmp_path = env
    dead = tmp_path / "tmp" / "livegrep-mcp-login-99999999-abc"
    alive = tmp_path / "tmp" / f"livegrep-mcp-login-{os.getpid()}-abc"
    odd = tmp_path / "tmp" / "livegrep-mcp-login-99999999999999999999-abc"  # not a pid
    target = tmp_path / "elsewhere"
    for d in (dead, alive, odd, target):
        (d / "Default").mkdir(parents=True)
    link = tmp_path / "tmp" / "livegrep-mcp-login-99999998-abc"
    link.symlink_to(target)

    async def scenario():
        async with session(base, tmp_path, LIVEGREP_CHROME="/nonexistent") as c:
            await c.list_tools()

    run(scenario())
    assert not dead.exists()
    assert alive.exists() and odd.exists()
    assert (target / "Default").exists()
