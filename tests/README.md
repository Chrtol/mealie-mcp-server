# Tests

Three test scripts: two integration scripts covering all 51 MCP tools at two
layers, plus a standalone unit test for the auth token verifier.

## Setup

```bash
cp tests/.env.testing.template tests/.env.testing
# Edit tests/.env.testing with your Mealie URL and a temporary API key
```

Create the temporary API key in Mealie under **Profile → API Tokens**. Delete it after testing.

## Script 1 — MealieFetcher (`test_fetcher.py`)

Tests every mixin method directly against the Mealie API. No MCP server required.

```bash
python tests/test_fetcher.py
```

**What it tests:**
- Every `MealieFetcher` method (correct endpoints, parameter mapping, response shapes)
- Full create → read → update → delete cycle for recipes, foods, shopping lists, and meal plans
- Cleanup of all test artifacts using `__test_*` naming convention

**Requirements:**
- `MEALIE_BASE_URL` and `MEALIE_API_KEY` set in `tests/.env.testing`
- External Mealie address (not internal Docker network)

## Script 2 — MCP Server (`test_mcp_server.py`)

Tests every tool via the MCP protocol against a running server instance.

```bash
docker compose -f tests/docker-compose.yml up -d --build
python tests/test_mcp_server.py
```

**What it tests:**
- All 51 tools are registered (`session.list_tools()`)
- Every tool can be called and returns a valid response
- Full create → read → update cycle via MCP tool calls
- Cleanup of test artifacts via direct fetcher calls (delete is not an MCP tool)

**Requirements:**
- MCP server running in Docker (`docker compose -f tests/docker-compose.yml up -d --build`)
- `MCP_SERVER_URL` set in `tests/.env.testing` (default: `http://localhost:8000/mcp`)
- Server's own `.env` must have valid `MEALIE_BASE_URL` and `MEALIE_API_KEY`

## Script 3 — Auth token verifier (`test_auth.py`)

Unit-tests `src/auth.py` (Authentik JWT verification). Needs **no live
infrastructure** — no Mealie, no Authentik, no running server, no
`.env.testing`. It mints its own RS256 keys with `joserfc` and stubs the JWKS
cache.

```bash
python tests/test_auth.py
```

**What it tests:**
- Audience enforcement — accepts a matching `aud`, rejects a wrong or missing one
- RS256 algorithm pinning — rejects an `HS256`-forged token (alg-confusion)
- Signing-key rotation — an unknown `kid` forces exactly one JWKS refetch
- Forced-refetch rate limiting (`_JWKS_MIN_REFETCH`)
- Claim validation — `exp`, `nbf`, and `iss` (incl. trailing-slash normalization)
- `build_token_verifier()` fails closed when `AUTHENTIK_AUDIENCE` is unset

**Requirements:**
- None beyond the project's own dependencies (`joserfc`, already required)

## Test naming convention

All test artifacts use `__test_*` naming so they are visually obvious in the Mealie UI:

| Artifact | Name |
|----------|------|
| Recipe | `__test_recipe__` → slug `test-recipe` |
| Recipe copy | `__test_recipe_copy__` → slug `test-recipe-copy` |
| Auto-food recipe | `__test_recipe_auto_food__` → slug `test-recipe-auto-food` |
| Food A | `__test_food_a__` |
| Food B | `__test_food_b__` |
| Auto-created food | `__test_auto_food__` |
| Shopping list | `__test_shopping_list__` |
| Meal plan | date `2099-01-01` |
