# KVSV Public Search Proxy

Backend proxy for the KVSV public search page (`public_search.html` in the main
[kvsv](https://github.com/kvsvsearch/kvsv) repo). Keeps third-party API keys
server-side and out of client-facing code.

## Why this exists

The public search page must pull only from live external public sources — never
from the private Research Database (`snapshot.json` / `masterDatabase` in the
main repo). This proxy calls those external sources server-side and returns
tagged, sourced results. Two separate tracks, by design.

## Endpoints

- `GET /api/wikitree-search?lastName=&firstName=` — WikiTree public profiles
  (Americas/Europe/Africa scope only, per project rules).
- `GET /api/nara-search?q=` — National Archives (NARA) Catalog API. Covers
  general federal records and Record Group 75 (Bureau of Indian Affairs),
  which holds the Dawes Rolls / Final Rolls enrollment cards for the Five
  Civilized Tribes.
- `GET /api/health` — health check.

## Environment variables

- `NARA_API_KEY` — required for `/api/nara-search`. Set this in the hosting
  platform's environment variable settings. Never commit it to source, and
  never expose it in any client-side/public-facing code.
- `PORT` — provided automatically by most hosting platforms (e.g. Railway).

## Running locally

```
pip install -r requirements.txt
NARA_API_KEY=your_key_here python server.py
```
