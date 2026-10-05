# Providers and request budgets

Provider credentials belong in the backend's ignored `.env` or in a user's encrypted model connection. Search-provider selection is server configuration; the traveler interface deliberately has no Brave/Tavily toggle.

## Travel search

| Provider | Environment key | Used for |
| --- | --- | --- |
| SerpAPI | `SERPAPI_API_KEY` | Google Flights and Hotels offers, airline logos, hotel photos and booking links |
| Brave | `BRAVE_API_KEY` | Default web evidence, attraction/place matching, structured place data and photos |
| Tavily | `TAVILY_API_KEY` | Web fallback when the default research search fails |

SerpAPI and Tavily keys are currently required by the orchestrator's initialization check. Brave is the default web/place provider and is needed for automatic Brave activity matching. Without a Brave key, web research can use its configured fallback; missing media remains absent rather than becoming a placeholder.

## AI models

### Personal models

The website exposes separate **orchestrator** and **researcher** roles with provider, model and reasoning effort. Personal API keys support OpenAI, Anthropic, Google and OpenRouter. The backend encrypts each user's keys and connection tokens in `.runtime/model-accounts`; they are not stored in browser local storage or included in committed configuration.

Supported subscription connections use a local sign-in workflow. ChatGPT's callback uses a loopback listener, so the browser and backend need to share the local connection environment. The optional Claude connection uses the installed CLI bridge:

```bash
npm ci --prefix model-bridge
```

An adapter being present does not guarantee that an account can access a model. Catalog availability and completed access checks determine which models the interface can offer. Connecting to a subscription is distinct from using a paid API key.

Website authentication is email/password or guest access. ChatGPT and Claude connections authorize **local model use**, independently of website login. Their account state includes explicit readiness, renewable/expired access and local bridge availability. Cancelling a sign-in closes the pending flow while retaining any existing credentials; disconnecting revokes/removes the connection.

Guest linking copies encrypted keys, OAuth registration/tokens, model-role choices and isolated Claude configuration before moving chat/message/run ownership. Existing destination credentials win. The source is retained until the ownership transaction commits. Active guest planning and pending model sign-in are blocked. The website records the verified source/destination relationship and can finish an interrupted link after sign-in; browser callers cannot supply a source identity to the generic backend proxy.

### Automatic and shared routing

Automatic selection prefers an eligible connected model account, with Sol preferred over Astra for the orchestrator, then available fallback routes. Researchers receive a lower role recommendation where the provider offers one. Recommendations use the current catalog and family heuristics rather than benchmark scores.

Shared Google and OpenRouter credentials are configured with `GOOGLE_API_KEY` and `OPENROUTER_API_KEY`. Their ordered fallback candidates are in [model_tiers.yaml](../trvelle/config/model_tiers.yaml). Researchers begin below the selected orchestrator tier when possible. The shared OpenRouter route validates free pricing and preview availability before use; a user's personal OpenRouter key can support their explicitly chosen paid model.

Explicit model assignments are strict. An unavailable selection reports its failure instead of silently switching to an unrelated model. Clear a role assignment to return to automatic routing. Provider model names and availability change; edit shared tiers or refresh the catalog rather than assuming a static README model list remains current.

### Cooldowns

Provider/model cooldowns are tied to the credential and persisted in `.runtime/model-routing.sqlite3`. Reported reset and retry times are honored, including ChatGPT token-refresh responses. An invalid refresh grant requires reconnection instead of repeating the same rejected grant. Temporary app timeouts are distinguished from provider quota resets. A cooling-down model is skipped, avoiding repeated requests that cannot succeed.

## Search cache and accounting

| Search type | Cached for |
| --- | --- |
| Google Flights | 15 minutes |
| Google Hotels / booking-offer lookup | 1 hour |
| Web research | 24 hours |
| Brave places / POIs | 7 hours |

The cache includes normalized query/filter data and a credential fingerprint. PostgreSQL locks coalesce concurrent duplicate work across the API, worker and tools. Usage reservations are saved before network calls. Known unbilled failures release their reservation; uncertain network outcomes remain conservatively counted.

Current configured account ceilings are **250 SerpAPI requests** and **1,000 Tavily credits**. A single-destination run starts with six of each; multiple-destination research can increase the bounded allowance to twelve. Manual detail operations start with two requests/credits per provider. Account limits still apply across all runs.

Brave controls are adjustable in `.env`:

```dotenv
BRAVE_MONTHLY_REQUEST_LIMIT=100
BRAVE_RUN_REQUEST_LIMIT=20
BRAVE_REQUESTS_PER_SECOND=1
BRAVE_ITINERARY_PLACE_LIMIT=8
```

The default run reserves eight Brave requests for place/photo enrichment, so web research cannot consume that portion first. Provider quota/reset headers are recorded as well. The authenticated `/search_configuration` endpoint reports configured providers, tracked usage, failures and reset windows.

These ceilings are application configuration, not provider plan entitlements. Changing a local limit does not increase the provider's real allowance.

## Developer-only provider comparison

To deliberately override search sources for testing:

```dotenv
TRVELLE_WEB_SEARCH_PROVIDER=tavily
TRVELLE_PLACE_SEARCH_PROVIDER=disabled
```

Defaults are `brave` for both. New runs snapshot the selected providers; resumed runs retain their snapshot. Manual lookups use the current backend configuration.

To compare the same four travel queries:

```bash
uv run --locked python scripts/compare-search-providers.py --live
```

The comparison caps spending at **four Tavily basic credits** and **twelve Brave requests**, reuses the shared cache, and calls neither SerpAPI nor an LLM. It writes `.runtime/reports/brave-vs-tavily.json` and `.md` and leaves defaults unchanged. Coverage metrics check names, locations and official domains; they do not verify every fact. Cached timings are not a live speed benchmark. The current Tavily adapter does not request images, which is not a claim that the provider has no image capability.

## Offline search replay

```dotenv
TRVELLE_SEARCH_MODE=replay
TRVELLE_REPLAY_DIR=tests/fixtures/search
```

Only recorded matching queries are returned; a missing fixture fails closed. Fixtures are historical search responses, not live availability. This mode replaces travel searches, not model inference; use the acceptance tests for validation with both search and model calls excluded.

Never commit `.env`, `.runtime`, model-account files or provider recordings containing credentials. The repository secret scanner checks source/staged content without printing detected values.
