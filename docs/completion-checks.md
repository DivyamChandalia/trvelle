# Local product acceptance — October 5, 2026

Website login is email/password or guest access. Password recovery is functional when the website has SMTP_URL and EMAIL_FROM; these are not configured in the ordinary local environment, so recovery is hidden there. Email verification is not required for the local release. Subscription connections remain separate, local model connections.

## Verification

The final automated checks passed: 210 backend tests, 12 website unit tests, eight browser acceptance tests, TypeScript, ESLint and a production Next.js build.

- Backend tests use a temporary PostgreSQL database and cover private credential migration, destination credential priority, source retention on failure, active planning/auth blocking, idempotent retries, cancellation, invalid refresh grants and provider refresh reset headers.
- Website unit tests cover validation, sanitized errors, recovery capability checks, streaming and chat state.
- Playwright uses a separate database/API/website and local SMTP server. It covers email login errors and retained fields, reset mail and session revocation, guest history/key transfer and ownership isolation, interrupted-link completion, session-expiry drafts, edits/previous itinerary versions, retained scroll, mobile focus, reduced motion and image failure layout.
- Current site screenshots were captured on desktop and phone widths with no browser exceptions or horizontal page overflow. Stored chats were only read for that visual review.

## Bounded live inference

The existing connected ChatGPT account was checked with GPT 6.1 Sol medium for the planner and GPT 6 Luna medium for research. Historical Singapore evidence was supplied, without issuing any new SerpAPI, Brave or Tavily requests. The researcher took 10.1 seconds; the planner took 91.1 seconds and returned a schema-valid five-day plan. The recorded INR 173,988 flight/stay subtotal remained intact within a requested INR 250,000 ceiling. Private room/bed configuration remained explicitly unverified. Saved hotel photos and offers were attached after generation.

An initial test incorrectly required the planner to reserialize 78 KB of saved offer metadata and exceeded its 240-second test budget. The corrected harness supplies 4.8 KB of compact evidence and follows the app's enrichment approach. That test limit does not create a model cooldown or imply a provider quota reset. The successful artifact is stored locally in ignored .runtime/acceptance-live-model.json; it is historical evidence, not a new bookable live quote.

## Local runtime

The application continues to use local PostgreSQL. The guest-link SQL migration was applied additively; saved accounts and chats were retained. The API, worker, tools and website remain independently runnable. SMTP is the only optional new external configuration. Hosted deployment, booking checkout and web-based provider identity login are outside this local release.
