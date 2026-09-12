# Drew Edge Board — Streamlit V4

This version changes the data plumbing to make matching reliable on Streamlit Community Cloud:

- Sports are always selectable from a fixed list.
- Only the sports you select are requested from The Odds API, conserving credits.
- Polymarket discovery uses official `/sports` metadata plus league-specific `/events`.
- Only Polymarket **YES** tokens are used. NO tokens are never interpreted as the opposing sportsbook side.
- Polymarket prices come from the official CLOB `POST /books` endpoint for **matched** YES tokens only.
- DraftKings and FanDuel are compared separately.
- Edge, EV, no-vig, Kelly, stake and maximum acceptable price are calculated live.
- Diagnostics shows exactly where discovery or matching fails.

## Streamlit setup

Upload `streamlit_app.py` and `requirements.txt` to the root of your GitHub repo.

In Streamlit Community Cloud, set the main file to:

`streamlit_app.py`

In **Manage app → Settings → Secrets**, add:

```toml
ODDS_API_KEY = "YOUR_KEY"
BANKROLL = 1000
```

Start with **NFL + Moneyline**. Once you see metadata matches and live priced matches, add Spread/Total and other sports.

The sportsbook response is cached for 60 seconds to reduce API-credit usage. Polymarket sports metadata is cached for 120 seconds, while matched CLOB order books refresh every ~5 seconds while the app is open.
