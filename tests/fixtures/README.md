# Recorded acceptance evidence

The search fixtures and Singapore itinerary were recorded on October 4, 2026
(Asia/Kolkata) using the existing ChatGPT connection: Sol medium for planning,
Luna medium for research. They retain flight logos, hotel photos, nearby places,
source links and suggested activity times. Credential fields and URLs are redacted.

These are historical quotes, not current availability. The live run reserved six
SerpAPI requests and three Tavily credits. A subsequent bounded hotel-filter check used one additional SerpAPI request.
Two unsuccessful hotel searches are not fixtures. `test_acceptance_replay.py` replays successful queries and checks the
saved itinerary without contacting search or model providers.

For deliberate tool replay, set `TRVELLE_SEARCH_MODE=replay` and
`TRVELLE_REPLAY_DIR=tests/fixtures/search`. Queries must match the manifest exactly;
missing fixtures fail closed. Replay mode does not disable model inference, so use
the automated replay test for completely offline validation.
