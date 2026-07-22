# Tests

Four automated scripts plus one manual LLM prompt:

- **Two unit scripts** — no live infrastructure (`test_auth.py`, `test_client_retry.py`)
- **Two integration scripts** — cover all 51 MCP tools at two layers
  (`test_fetcher.py` direct, `test_mcp_server.py` via the MCP protocol)
- **One manual LLM prompt** — drives a model to call every read-only tool
  (`LLM_PROMPT.md`)

## Recommended run order

Run the unit scripts first (fast, touch nothing), then the integration scripts,
then the manual LLM prompt:

```bash
python tests/test_auth.py            # unit — no infra
python tests/test_client_retry.py    # unit — no infra
python tests/test_fetcher.py         # integration — live Mealie
docker compose -f tests/docker-compose.yml up -d --build
python tests/test_mcp_server.py      # integration — live Mealie via MCP
docker compose -f tests/docker-compose.yml down
# then paste tests/LLM_PROMPT.md into Claude/ChatGPT with the MCP server connected
```

> The integration scripts create and delete real recipes/foods/lists/meal plans.
> They are self-cleaning within a single run and everything is `__test_*`-named,
> so they are safe to run against a production Mealie — just use a temporary API
> key and delete it afterwards.

## Setup

Only the integration scripts (1 and 2) need this; the unit scripts and the LLM
prompt do not.

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

## Script 4 — Request retry (`test_client_retry.py`)

Unit-tests the `RemoteProtocolError` retry logic in `src/mealie/client.py`. Needs
**no live infrastructure** — it builds a `MealieClient` with a stubbed httpx client
(bypassing the constructor's connection check) and drives `_handle_request` directly.

```bash
python tests/test_client_retry.py
```

**What it tests:**
- An idempotent method (GET/PATCH/DELETE) is retried once on a stale-connection
  error and can succeed on the retry
- A non-idempotent **POST** is **not** retried — it raises after exactly one attempt,
  so a request already processed server-side is never duplicated (e.g. `recipe` /
  `recipe-1`)

**Requirements:**
- None beyond the project's own dependencies (`httpx`, already required)

## Manual — LLM integration prompt (`LLM_PROMPT.md`)

Not a script. Paste the body of `tests/LLM_PROMPT.md` into Claude or ChatGPT with
the Mealie MCP server connected. It instructs the model to call every **read-only**
tool once — chaining IDs/slugs from earlier calls — and report PASS/FAIL per tool.

**What it tests:**
- Every read-only tool is reachable and returns a valid response through a real
  LLM client (the actual usage path), not just the protocol

**Requirements:**
- The MCP server connected to an LLM client (no `.env.testing` needed)
- Entirely read-only — the safest test to run against production. Note ChatGPT's
  safety filter may false-positive on some read-only tools (see the file); treat
  those as platform noise, not server failures.

## Test naming convention

All test artifacts use `__test_*` naming so they are visually obvious in the Mealie UI.
The integration scripts delete every one of these within a single run:

| Artifact | Name |
|----------|------|
| Recipe | `__test_recipe__` → slug `test-recipe` |
| Recipe copy | `__test_recipe_copy__` → slug `test-recipe-copy` |
| Auto-food recipe | `__test_recipe_auto_food__` → slug `test-recipe-auto-food` |
| Fractional recipe | `__test_recipe_fractional__` → slug `test-recipe-fractional` |
| Unknown-unit recipe | `__test_recipe_unknown_unit__` → slug `test-recipe-unknown-unit` |
| Food A / B | `__test_food_a__` / `__test_food_b__` |
| Auto-created food | `__test_auto_food__` |
| Unknown-unit foods | `__test_food_uu_a__` / `__test_food_uu_b__` |
| Shopping list | `__test_shopping_list__` |
| Meal plan | date `2099-01-01` |
