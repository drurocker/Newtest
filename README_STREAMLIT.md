# Drew Edge Board — Streamlit Edition

## GitHub files
Upload these to your repository root:

- `streamlit_app.py`
- `requirements.txt`
- `.gitignore`
- `.streamlit/config.toml`

You may leave the older FastAPI files in the repo, but Streamlit will use `streamlit_app.py` as the entrypoint.

## Deploy on Streamlit Community Cloud
1. Go to Streamlit Community Cloud.
2. Create a new app from GitHub.
3. Repository: `drurocker/DrewEdge`
4. Branch: `main`
5. Main file path: `streamlit_app.py`
6. Before launching, open **Advanced settings / Secrets** and add:

```toml
ODDS_API_KEY = "YOUR_THE_ODDS_API_KEY"
BANKROLL = 1000
KELLY_FRACTION = 0.25
SPORTSBOOK_POLL_SECONDS = 60
POLY_DISCOVERY_SECONDS = 300
```

7. Deploy.

## What V1 includes
- Polymarket live WebSocket quotes
- DraftKings + FanDuel via The Odds API
- Poly fair probability from best bid/ask midpoint
- Raw sportsbook break-even probability
- No-vig probability shown separately
- Edge in percentage points
- Expected value / expected ROI
- Full and fractional Kelly
- Suggested dollar stake based on bankroll
- Maximum acceptable sportsbook price
- Quote-age and Polymarket-spread quality gates
- PASS / WATCH / EDGE / STRONG EDGE signals
- NFL, NBA, MLB, NHL, UFC/MMA
- Moneyline, spread, total
- 2-second dashboard UI refresh while the app is active
- Session signal history + CSV download

## Important limitation
Streamlit Community Cloud can put an inactive app to sleep. That means this V1 is excellent as a dashboard while you have it open, but it is not yet a guaranteed 24/7 historical data collector. For continuous lead/lag logging, we can later move the collector/database to a persistent backend while keeping Streamlit as the front end.


## V1.1 sportsbook-key fix

If Streamlit Secrets has not been configured, the sidebar now includes a password field for **The Odds API key**. Paste the key there and the DraftKings/FanDuel polling engine starts immediately for that browser session. For a permanent setup, store `ODDS_API_KEY` in Streamlit Community Cloud → Manage app → Settings → Secrets.

## Sept 12 live-board matching fix

This build also fixes two issues discovered during live testing:

- Polymarket WebSocket subscriptions now use the documented `operation: subscribe` update message after the first 500 tokens, so the board can receive quotes beyond the first subscription batch.
- Sportsbook team selections are matched correctly to Polymarket's common binary `Yes/No` sports propositions by using the market question/title instead of comparing the literal word `Yes` to a team name.
- The Sports selector is populated directly from loaded sportsbook events, so NFL/NBA/etc. remain selectable while Poly/book matching warms up.
