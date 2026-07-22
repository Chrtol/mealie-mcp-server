"""
Script 4: Request-retry unit tests.

Like test_auth.py, this needs NO live infrastructure — no Mealie, no running
MCP server. It builds a MealieClient with a stubbed httpx client (bypassing the
constructor's live connection check) and drives _handle_request directly to lock
in the retry behavior added in 1.0.21:

  - a RemoteProtocolError on an idempotent method (GET/PATCH/DELETE) is retried
    once with a fresh connection, and can succeed on the retry
  - a RemoteProtocolError on a non-idempotent POST is NOT retried, because the
    request may already have been processed server-side and a blind retry would
    create a duplicate resource (e.g. "recipe" and "recipe-1")

Usage:
    cd mealie-mcp-server
    python tests/test_client_retry.py
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from httpx import RemoteProtocolError

from mealie.client import MealieClient

PASS = "\033[92mPASS\033[0m"
FAIL = "\033[91mFAIL\033[0m"

results: list[tuple[str, bool, str]] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    status = PASS if condition else FAIL
    print(f"  [{status}] {name}" + (f" — {detail}" if detail else ""))
    results.append((name, bool(condition), detail))


def section(title: str) -> None:
    print(f"\n{'='*60}\n  {title}\n{'='*60}")


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------
class _FakeResponse:
    """Minimal stand-in for httpx.Response covering what _handle_request reads."""

    def __init__(self, payload: dict):
        self._payload = payload
        self.status_code = 200
        self.content = b'{"ok": true}'

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeHTTPClient:
    """Records calls and replays a scripted list of behaviors per .request()."""

    def __init__(self, behaviors):
        # behaviors: list where each item is either an Exception to raise or a
        # _FakeResponse to return, consumed in order across successive calls.
        self._behaviors = list(behaviors)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url))
        behavior = self._behaviors.pop(0)
        if isinstance(behavior, Exception):
            raise behavior
        return behavior


def make_client(behaviors) -> tuple[MealieClient, _FakeHTTPClient]:
    """Build a MealieClient without running __init__ (which would hit the network)."""
    client = object.__new__(MealieClient)
    client.base_url = "http://test"
    fake = _FakeHTTPClient(behaviors)
    client._client = fake
    return client, fake


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
def run() -> None:
    section("Idempotent methods retry once on RemoteProtocolError")

    # GET: fails once with a stale-connection error, then succeeds on retry.
    client, fake = make_client([
        RemoteProtocolError("server disconnected"),
        _FakeResponse({"slug": "chicken-soup"}),
    ])
    try:
        result = client._handle_request("GET", "/api/recipes")
        check("GET retries and succeeds on the second attempt", result == {"slug": "chicken-soup"}, str(result))
        check("GET issued exactly two requests (original + retry)", len(fake.calls) == 2, f"calls={len(fake.calls)}")
    except Exception as e:
        check("GET retries and succeeds on the second attempt", False, repr(e))

    # PATCH is idempotent too — it should also retry.
    client, fake = make_client([
        RemoteProtocolError("server disconnected"),
        _FakeResponse({"ok": True}),
    ])
    try:
        client._handle_request("PATCH", "/api/recipes/x")
        check("PATCH retries (idempotent)", len(fake.calls) == 2, f"calls={len(fake.calls)}")
    except Exception as e:
        check("PATCH retries (idempotent)", False, repr(e))

    section("Non-idempotent POST is NOT retried")

    # POST: a single RemoteProtocolError must surface as an error WITHOUT a retry,
    # because the recipe may already have been created server-side.
    client, fake = make_client([
        RemoteProtocolError("server disconnected"),
        # A second _FakeResponse is scripted but must never be consumed:
        _FakeResponse({"slug": "should-not-happen"}),
    ])
    raised = False
    try:
        client._handle_request("POST", "/api/recipes")
    except ConnectionError:
        raised = True
    except Exception as e:
        check("POST raises without retrying", False, f"unexpected error: {e!r}")
    check("POST raises ConnectionError on RemoteProtocolError", raised)
    check("POST issued exactly one request (no retry → no duplicate)", len(fake.calls) == 1, f"calls={len(fake.calls)}")

    section("Summary")
    passed = sum(1 for _, ok, _ in results if ok)
    failed = sum(1 for _, ok, _ in results if not ok)
    total = len(results)
    print(f"\n  {passed}/{total} passed", end="")
    if failed:
        print(f"  ({failed} failed)\n\nFailed tests:")
        for name, ok, detail in results:
            if not ok:
                print(f"  - {name}" + (f": {detail}" if detail else ""))
    else:
        print(" — all tests passed")
    print()
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    run()
