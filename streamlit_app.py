import asyncio
import hashlib
import json
import os
import re
import threading
import time
from collections import deque
from datetime import datetime, timezone
from difflib import SequenceMatcher
from typing import Any

import httpx
import pandas as pd
import streamlit as st
import websockets

# ------------------------------
# App configuration
# ------------------------------
st.set_page_config(
    page_title="Drew Edge Board",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded",
)

GAMMA = "https://gamma-api.polymarket.com"
POLY_WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
ODDS_BASE = "https://api.the-odds-api.com/v4"

DEFAULT_SPORT_KEYS = [
    "americanfootball_nfl",
    "basketball_nba",
    "baseball_mlb",
    "icehockey_nhl",
    "mma_mixed_martial_arts",
]
BOOKMAKERS = ["draftkings", "fanduel"]
ODDS_MARKETS = ["h2h", "spreads", "totals"]

SPORT_LABELS = {
    "americanfootball_nfl": "NFL",
    "basketball_nba": "NBA",
    "baseball_mlb": "MLB",
    "icehockey_nhl": "NHL",
    "mma_mixed_martial_arts": "UFC/MMA",
    "americanfootball_ncaaf": "NCAAF",
    "basketball_ncaab": "NCAAB",
}
BOOK_LABELS = {"draftkings": "DK", "fanduel": "FD"}
MARKET_LABELS = {"h2h": "Moneyline", "spreads": "Spread", "totals": "Total"}


def secret(name: str, default: Any = "") -> Any:
    try:
        if name in st.secrets:
            return st.secrets[name]
    except Exception:
        pass
    return os.getenv(name, default)


def to_float(v, default):
    try:
        return float(v)
    except Exception:
        return default


def to_int(v, default):
    try:
        return int(v)
    except Exception:
        return default


ODDS_API_KEY = str(secret("ODDS_API_KEY", "")).strip()
POLL_SECONDS = max(30, to_int(secret("SPORTSBOOK_POLL_SECONDS", 60), 60))
DISCOVERY_SECONDS = max(120, to_int(secret("POLY_DISCOVERY_SECONDS", 300), 300))
DEFAULT_BANKROLL = to_float(secret("BANKROLL", 1000), 1000)
DEFAULT_KELLY_FRAC = to_float(secret("KELLY_FRACTION", 0.25), 0.25)


# ------------------------------
# Math + matching helpers
# ------------------------------
def now_ts() -> float:
    return time.time()


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_jsonish(v, default=None):
    if v is None:
        return default
    if isinstance(v, (list, dict)):
        return v
    try:
        return json.loads(v)
    except Exception:
        return default


def parse_dt(v):
    if not v:
        return None
    try:
        if isinstance(v, (int, float)):
            x = float(v)
            return x / 1000 if x > 10_000_000_000 else x
        s = str(v)
        if s.isdigit():
            x = float(s)
            return x / 1000 if x > 10_000_000_000 else x
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def american_to_decimal(o: float) -> float:
    return 1 + (o / 100 if o > 0 else 100 / abs(o))


def implied_prob(o: float) -> float:
    return 1 / american_to_decimal(o)


def american_from_prob(p: float) -> int | None:
    if not (0 < p < 1):
        return None
    if p >= 0.5:
        return round(-100 * p / (1 - p))
    return round(100 * (1 - p) / p)


def calc_metrics(poly_p: float, odds: int, bankroll: float, kelly_fraction: float):
    dec = american_to_decimal(odds)
    b = dec - 1
    q = 1 - poly_p
    be = 1 / dec
    ev = poly_p * dec - 1
    k = (b * poly_p - q) / b if b > 0 else -1
    k_clip = max(0.0, k)
    return {
        "break_even": be,
        "edge": poly_p - be,
        "ev": ev,
        "full_kelly": k_clip,
        "fractional_kelly": k_clip * kelly_fraction,
        "stake": bankroll * k_clip * kelly_fraction,
        "max_price": american_from_prob(poly_p),
    }


def norm_text(s: str) -> str:
    s = (s or "").lower()
    replacements = {
        "@": " ", " at ": " ", " vs. ": " ", " vs ": " ", "-": " ",
        ".": " ", "'": "", "&": " and ",
    }
    for a, b in replacements.items():
        s = s.replace(a, b)
    s = re.sub(r"\b(fc|cf|the)\b", " ", s)
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def token_set_ratio(a: str, b: str) -> float:
    ta = set((a or "").split())
    tb = set((b or "").split())
    if not ta and not tb:
        return 100.0
    if not ta or not tb:
        return 0.0
    common = sorted(ta & tb)
    left = sorted(ta - tb)
    right = sorted(tb - ta)
    common_s = " ".join(common)
    left_s = " ".join(common + left)
    right_s = " ".join(common + right)

    def ratio(x: str, y: str) -> float:
        return 100.0 * SequenceMatcher(None, x, y).ratio()

    scores = [ratio(left_s, right_s)]
    if common_s:
        scores.extend([ratio(common_s, left_s), ratio(common_s, right_s)])
    return max(scores)


def match_score(poly_text: str, home: str, away: str) -> float:
    p = norm_text(poly_text)
    h = norm_text(home)
    a = norm_text(away)
    return (token_set_ratio(p, h) + token_set_ratio(p, a)) / 2


def stable_key(*parts: Any) -> str:
    raw = "|".join("" if x is None else str(x) for x in parts)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def best_pair(quote):
    bid = quote.get("best_bid")
    ask = quote.get("best_ask")
    if bid is None or ask is None or ask < bid:
        return None
    return (bid + ask) / 2


# ------------------------------
# Background live-data engine
# ------------------------------
class EdgeEngine:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.lock = threading.RLock()
        self.started = False
        self.thread = None
        self.poly_markets: dict[str, dict] = {}
        self.poly_quotes: dict[str, dict] = {}
        self.sportsbook_events: dict[str, dict] = {}
        self.base_rows: list[dict] = []
        self.errors = deque(maxlen=40)
        self.signal_history = deque(maxlen=500)
        self.signal_state: dict[str, str] = {}
        self.connections = {
            "poly": "starting",
            "sportsbook": "no_api_key" if not api_key else "starting",
        }
        self.last_poly_discovery = None
        self.last_book_poll = None
        self.started_at = now_ts()

    def start(self):
        with self.lock:
            if self.started:
                return
            self.started = True
            self.thread = threading.Thread(target=self._thread_main, daemon=True, name="drew-edge-feed")
            self.thread.start()

    def _thread_main(self):
        try:
            asyncio.run(self._runner())
        except Exception as exc:
            self._err(f"Feed engine stopped: {exc}")
            with self.lock:
                self.started = False

    async def _runner(self):
        tasks = [asyncio.create_task(self.discover_poly()), asyncio.create_task(self.poly_stream())]
        if self.api_key:
            tasks.append(asyncio.create_task(self.sportsbook_poll()))
        await asyncio.gather(*tasks)

    def _err(self, msg: str):
        with self.lock:
            self.errors.appendleft({"ts": iso_now(), "message": str(msg)})

    async def discover_poly(self):
        while True:
            try:
                markets = {}
                async with httpx.AsyncClient(timeout=25) as client:
                    for mtype in ("moneyline", "spreads", "totals"):
                        cursor = None
                        for _ in range(6):
                            params = {"closed": "false", "limit": 100, "sports_market_types": mtype}
                            if cursor:
                                params["after_cursor"] = cursor
                            r = await client.get(f"{GAMMA}/markets/keyset", params=params)
                            r.raise_for_status()
                            payload = r.json()
                            items = payload.get("markets", []) if isinstance(payload, dict) else payload
                            for m in items:
                                toks = parse_jsonish(m.get("clobTokenIds") or m.get("clob_token_ids"), []) or []
                                outs = parse_jsonish(m.get("outcomes"), []) or []
                                if isinstance(outs, dict):
                                    outs = list(outs.keys())
                                if not toks or len(toks) != len(outs):
                                    continue
                                sports = m.get("sports") or {}
                                game_time = m.get("gameStartTime") or sports.get("gameStartTime") or m.get("startDate")
                                title = " ".join(str(x) for x in [m.get("question"), m.get("groupItemTitle"), m.get("slug")] if x)
                                line = m.get("line") or sports.get("line")
                                mt = m.get("sportsMarketType") or sports.get("sportsMarketType") or mtype
                                for tok, out in zip(toks, outs):
                                    markets[str(tok)] = {
                                        "token_id": str(tok),
                                        "market_id": str(m.get("id", "")),
                                        "condition_id": m.get("conditionId"),
                                        "title": title,
                                        "selection": str(out),
                                        "market_type": mt,
                                        "line": line,
                                        "game_start": parse_dt(game_time),
                                    }
                            cursor = payload.get("next_cursor") if isinstance(payload, dict) else None
                            if not cursor:
                                break
                with self.lock:
                    self.poly_markets = markets
                    self.last_poly_discovery = iso_now()
            except Exception as e:
                self._err(f"Polymarket discovery: {e}")
            await asyncio.sleep(DISCOVERY_SECONDS)

    async def poly_stream(self):
        while True:
            with self.lock:
                toks = list(self.poly_markets.keys())
            if not toks:
                with self.lock:
                    self.connections["poly"] = "waiting"
                await asyncio.sleep(4)
                continue
            try:
                with self.lock:
                    self.connections["poly"] = "connecting"
                async with websockets.connect(POLY_WS, ping_interval=None, close_timeout=5, max_size=8_000_000) as ws:
                    with self.lock:
                        self.connections["poly"] = "live"
                    for i in range(0, len(toks), 500):
                        await ws.send(json.dumps({
                            "assets_ids": toks[i:i + 500],
                            "type": "market",
                            "custom_feature_enabled": True,
                        }))

                    async def heartbeat():
                        while True:
                            await asyncio.sleep(10)
                            await ws.send("PING")

                    hb = asyncio.create_task(heartbeat())
                    try:
                        async for raw in ws:
                            if raw in ("PONG", "pong"):
                                continue
                            msg = json.loads(raw)
                            items = msg if isinstance(msg, list) else [msg]
                            changed = False
                            with self.lock:
                                for x in items:
                                    typ = x.get("event_type") or x.get("type")
                                    if typ == "book":
                                        tok = str(x.get("asset_id") or x.get("token_id") or "")
                                        bids = x.get("bids") or []
                                        asks = x.get("asks") or []
                                        bid = max((float(v["price"]) for v in bids), default=None)
                                        ask = min((float(v["price"]) for v in asks), default=None)
                                        depth_bid = sum(float(v.get("size", 0)) for v in bids[:5])
                                        depth_ask = sum(float(v.get("size", 0)) for v in asks[:5])
                                        self.poly_quotes[tok] = {
                                            "best_bid": bid,
                                            "best_ask": ask,
                                            "spread": (ask - bid if bid is not None and ask is not None else None),
                                            "last_trade": float(x.get("last_trade_price")) if x.get("last_trade_price") else None,
                                            "ts": parse_dt(x.get("timestamp")) or now_ts(),
                                            "depth_bid": depth_bid,
                                            "depth_ask": depth_ask,
                                        }
                                        changed = True
                                    elif typ == "best_bid_ask":
                                        tok = str(x.get("asset_id") or "")
                                        q = self.poly_quotes.setdefault(tok, {})
                                        q.update({
                                            "best_bid": float(x["best_bid"]) if x.get("best_bid") else None,
                                            "best_ask": float(x["best_ask"]) if x.get("best_ask") else None,
                                            "spread": float(x["spread"]) if x.get("spread") else None,
                                            "ts": parse_dt(x.get("timestamp")) or now_ts(),
                                        })
                                        changed = True
                                    elif typ == "price_change":
                                        for pc in x.get("price_changes", []):
                                            tok = str(pc.get("asset_id") or "")
                                            q = self.poly_quotes.setdefault(tok, {})
                                            if pc.get("best_bid") is not None:
                                                q["best_bid"] = float(pc["best_bid"])
                                            if pc.get("best_ask") is not None:
                                                q["best_ask"] = float(pc["best_ask"])
                                            if q.get("best_bid") is not None and q.get("best_ask") is not None:
                                                q["spread"] = q["best_ask"] - q["best_bid"]
                                            q["ts"] = parse_dt(x.get("timestamp")) or now_ts()
                                            changed = True
                                    elif typ == "last_trade_price":
                                        tok = str(x.get("asset_id") or "")
                                        q = self.poly_quotes.setdefault(tok, {})
                                        if x.get("price") is not None:
                                            q["last_trade"] = float(x.get("price"))
                                        q["ts"] = parse_dt(x.get("timestamp")) or now_ts()
                                        changed = True
                            if changed:
                                self.rebuild_base_rows()
                    finally:
                        hb.cancel()
            except Exception as e:
                with self.lock:
                    self.connections["poly"] = "reconnecting"
                self._err(f"Polymarket WS: {e}")
                await asyncio.sleep(3)

    async def sportsbook_poll(self):
        async with httpx.AsyncClient(timeout=25) as client:
            while True:
                try:
                    all_events = {}
                    for sport in DEFAULT_SPORT_KEYS:
                        params = {
                            "apiKey": self.api_key,
                            "bookmakers": ",".join(BOOKMAKERS),
                            "markets": ",".join(ODDS_MARKETS),
                            "oddsFormat": "american",
                            "dateFormat": "iso",
                        }
                        r = await client.get(f"{ODDS_BASE}/sports/{sport}/odds", params=params)
                        if r.status_code in (404, 422):
                            continue
                        r.raise_for_status()
                        for ev in r.json():
                            start = parse_dt(ev.get("commence_time"))
                            day = datetime.fromtimestamp(start, timezone.utc).strftime("%Y-%m-%d") if start else "na"
                            ek = f"{sport}|{norm_text(ev.get('away_team',''))}|{norm_text(ev.get('home_team',''))}|{day}"
                            ev["_sport"] = sport
                            ev["_start_ts"] = start
                            all_events[ek] = ev
                    with self.lock:
                        self.sportsbook_events = all_events
                        self.last_book_poll = iso_now()
                        self.connections["sportsbook"] = "live"
                    self.rebuild_base_rows()
                except Exception as e:
                    with self.lock:
                        self.connections["sportsbook"] = "error"
                    self._err(f"Sportsbook poll: {e}")
                await asyncio.sleep(POLL_SECONDS)

    def find_poly_for_event(self, ev, book_market, book_outcome):
        target_type = {"h2h": "moneyline", "spreads": "spreads", "totals": "totals"}.get(book_market)
        if not target_type:
            return None
        best = None
        with self.lock:
            items = list(self.poly_markets.items())
        for tok, m in items:
            mt = str(m.get("market_type") or "").lower()
            if target_type not in mt:
                continue
            if m.get("game_start") and ev.get("_start_ts") and abs(m["game_start"] - ev["_start_ts"]) > 6 * 3600:
                continue
            score = match_score(m.get("title", ""), ev.get("home_team", ""), ev.get("away_team", ""))
            sel_score = token_set_ratio(norm_text(m.get("selection", "")), norm_text(book_outcome.get("name", "")))
            point = book_outcome.get("point")
            if book_market in ("spreads", "totals") and point is not None and m.get("line") is not None:
                try:
                    if abs(abs(float(point)) - abs(float(m["line"]))) > 0.01:
                        continue
                except Exception:
                    pass
            combined = 0.75 * score + 0.25 * sel_score
            if combined >= 70 and (best is None or combined > best[0]):
                best = (combined, tok, m)
        return best

    def rebuild_base_rows(self):
        rows = []
        now = now_ts()
        with self.lock:
            events = list(self.sportsbook_events.values())
            quotes = dict(self.poly_quotes)
        for ev in events:
            game = f"{ev.get('away_team')} @ {ev.get('home_team')}"
            for bk in ev.get("bookmakers", []):
                book = bk.get("key")
                if book not in BOOKMAKERS:
                    continue
                bts = parse_dt(bk.get("last_update")) or now
                age = max(0, now - bts)
                for mk in bk.get("markets", []):
                    market = mk.get("key")
                    outs = mk.get("outcomes", [])
                    implieds = [implied_prob(float(o["price"])) for o in outs if o.get("price")]
                    denom = sum(implieds) if implieds else 0
                    for out in outs:
                        if out.get("price") is None:
                            continue
                        found = self.find_poly_for_event(ev, market, out)
                        if not found:
                            continue
                        mscore, tok, _pm = found
                        q = quotes.get(tok)
                        if not q:
                            continue
                        poly = best_pair(q)
                        if poly is None:
                            continue
                        no_vig = implied_prob(float(out["price"])) / denom if denom else None
                        poly_age = max(0, now - (q.get("ts") or now))
                        watch_key = stable_key(ev.get("_sport"), game, market, out.get("name"), out.get("point"))
                        row_key = stable_key(watch_key, book)
                        rows.append({
                            "row_key": row_key,
                            "watch_key": watch_key,
                            "sport": ev.get("_sport"),
                            "sport_label": SPORT_LABELS.get(ev.get("_sport"), ev.get("_sport")),
                            "game": game,
                            "start": ev.get("commence_time"),
                            "market": market,
                            "selection": out.get("name"),
                            "point": out.get("point"),
                            "book": book,
                            "poly_prob": poly,
                            "poly_bid": q.get("best_bid"),
                            "poly_ask": q.get("best_ask"),
                            "poly_spread": q.get("spread"),
                            "poly_depth_bid": q.get("depth_bid"),
                            "poly_depth_ask": q.get("depth_ask"),
                            "poly_last_trade": q.get("last_trade"),
                            "odds": int(out["price"]),
                            "no_vig": no_vig,
                            "book_age": age,
                            "poly_age": poly_age,
                            "match_score": mscore,
                        })
        with self.lock:
            self.base_rows = rows[:2500]

    def snapshot(self):
        with self.lock:
            return {
                "rows": [dict(x) for x in self.base_rows],
                "connections": dict(self.connections),
                "last_poly_discovery": self.last_poly_discovery,
                "last_book_poll": self.last_book_poll,
                "poly_market_count": len(self.poly_markets),
                "poly_quote_count": len(self.poly_quotes),
                "sportsbook_event_count": len(self.sportsbook_events),
                "errors": list(self.errors),
                "signal_history": list(self.signal_history),
                "uptime": now_ts() - self.started_at,
            }

    def record_signals(self, calculated_rows: list[dict]):
        with self.lock:
            for row in calculated_rows:
                key = row["row_key"]
                cur = row["signal"]
                prev = self.signal_state.get(key)
                if prev == cur:
                    continue
                self.signal_state[key] = cur
                if prev is None and cur == "PASS":
                    continue
                self.signal_history.appendleft({
                    "time": datetime.now().strftime("%H:%M:%S"),
                    "sport": row["sport_label"],
                    "game": row["game"],
                    "market": MARKET_LABELS.get(row["market"], row["market"]),
                    "selection": row["selection"],
                    "book": BOOK_LABELS.get(row["book"], row["book"]),
                    "previous": prev or "NEW",
                    "signal": cur,
                    "edge": row["edge"],
                    "ev": row["ev"],
                    "odds": row["odds"],
                })


@st.cache_resource(show_spinner=False)
def get_engine(api_key: str):
    eng = EdgeEngine(api_key)
    eng.start()
    return eng


engine = get_engine(ODDS_API_KEY)


# ------------------------------
# UI helpers
# ------------------------------
st.markdown(
    """
    <style>
    .block-container {padding-top: 1rem; padding-bottom: 2rem; max-width: 1600px;}
    [data-testid="stMetricValue"] {font-size: 1.45rem;}
    .edge-title {font-weight:800;font-size:1.7rem;letter-spacing:.02em;margin-bottom:.1rem}
    .subtle {color:#8b98a9;font-size:.9rem}
    .statusline {padding:.45rem .65rem;border:1px solid rgba(128,128,128,.25);border-radius:.65rem;margin:.2rem 0 .8rem 0;}
    .signal-strong {color:#40df8a;font-weight:800}
    .signal-edge {color:#7ee6a7;font-weight:700}
    .signal-watch {color:#ffd166;font-weight:700}
    .signal-pass {color:#ff7b7b;font-weight:700}
    </style>
    """,
    unsafe_allow_html=True,
)


def fmt_odds(x):
    try:
        n = int(x)
        return f"+{n}" if n > 0 else str(n)
    except Exception:
        return "—"


def signal_for(row, bankroll, kelly_fraction, max_poly_spread, max_book_age, max_poly_age, min_ev, strong_ev, strong_edge_pp):
    m = calc_metrics(row["poly_prob"], row["odds"], bankroll, kelly_fraction)
    flags = []
    spread = row.get("poly_spread")
    if spread is not None and spread > max_poly_spread:
        flags.append("WIDE POLY SPREAD")
    if row.get("book_age", 0) > max_book_age:
        flags.append("STALE BOOK")
    if row.get("poly_age", 0) > max_poly_age:
        flags.append("STALE POLY")
    if row.get("match_score", 0) < 70:
        flags.append("LOW MATCH CONFIDENCE")

    if flags or m["ev"] <= 0 or m["full_kelly"] <= 0:
        sig = "PASS"
    elif m["ev"] < min_ev:
        sig = "WATCH"
    elif m["ev"] >= strong_ev and m["edge"] * 100 >= strong_edge_pp:
        sig = "STRONG EDGE"
    else:
        sig = "EDGE"
    return {**row, **m, "quality_flags": flags, "signal": sig}


# Sidebar controls
st.sidebar.markdown("## ⚙️ Edge Settings")
bankroll = st.sidebar.number_input("Bankroll ($)", min_value=1.0, value=float(DEFAULT_BANKROLL), step=50.0)
kelly_choice = st.sidebar.selectbox("Kelly sizing", ["1/8 Kelly", "1/4 Kelly", "1/2 Kelly", "Full Kelly"], index=1)
kelly_fraction = {"1/8 Kelly": 0.125, "1/4 Kelly": 0.25, "1/2 Kelly": 0.5, "Full Kelly": 1.0}[kelly_choice]
min_ev_pct = st.sidebar.slider("Minimum EV for EDGE", 0.0, 10.0, 3.0, 0.5)
strong_ev_pct = st.sidebar.slider("STRONG EDGE EV", 1.0, 15.0, 5.0, 0.5)
strong_edge_pp = st.sidebar.slider("STRONG EDGE probability edge (pp)", 0.5, 8.0, 2.0, 0.5)
max_poly_spread = st.sidebar.slider("Max Poly spread", 0.01, 0.15, 0.05, 0.01)
max_book_age = st.sidebar.slider("Max sportsbook quote age (sec)", 30, 240, 95, 5)
max_poly_age = st.sidebar.slider("Max Poly quote age (sec)", 10, 120, 30, 5)

st.sidebar.markdown("---")
st.sidebar.caption(f"Sportsbook polling: every {POLL_SECONDS}s (provider freshness may be slower).")
if not ODDS_API_KEY:
    st.sidebar.error("Missing ODDS_API_KEY. Add it in Streamlit → App settings → Secrets.")


# Header
st.markdown('<div class="edge-title">DREW EDGE BOARD</div>', unsafe_allow_html=True)
st.markdown('<div class="subtle">Polymarket × DraftKings × FanDuel • Edge • EV • Kelly • live quote quality</div>', unsafe_allow_html=True)


@st.fragment(run_every="2s")
def live_board():
    snap = engine.snapshot()
    conn = snap["connections"]
    poly_live = conn.get("poly") == "live"
    book_live = conn.get("sportsbook") == "live"

    st.markdown(
        f'<div class="statusline">'
        f'Polymarket: <b>{"🟢 LIVE" if poly_live else "🟡 " + str(conn.get("poly"))}</b> &nbsp;•&nbsp; '
        f'Sportsbooks: <b>{"🟢 LIVE" if book_live else "🟡 " + str(conn.get("sportsbook"))}</b> &nbsp;•&nbsp; '
        f'Poly markets: <b>{snap["poly_market_count"]}</b> &nbsp;•&nbsp; '
        f'Poly quotes: <b>{snap["poly_quote_count"]}</b> &nbsp;•&nbsp; '
        f'Book events: <b>{snap["sportsbook_event_count"]}</b>'
        f'</div>',
        unsafe_allow_html=True,
    )

    calculated = [
        signal_for(
            r, bankroll, kelly_fraction, max_poly_spread, max_book_age, max_poly_age,
            min_ev_pct / 100, strong_ev_pct / 100, strong_edge_pp,
        )
        for r in snap["rows"]
    ]
    engine.record_signals(calculated)

    # Filters
    with st.container(border=True):
        c1, c2, c3, c4, c5 = st.columns([1.2, 1.3, 1.1, 1.2, 1.2])
        sport_options = sorted({r["sport_label"] for r in calculated})
        selected_sports = c1.multiselect("Sports", sport_options, default=sport_options)
        market_options = ["Moneyline", "Spread", "Total"]
        selected_markets = c2.multiselect("Markets", market_options, default=market_options)
        signal_options = ["STRONG EDGE", "EDGE", "WATCH", "PASS"]
        selected_signals = c3.multiselect("Signals", signal_options, default=signal_options[:3])
        book_options = ["DK", "FD"]
        selected_books = c4.multiselect("Books", book_options, default=book_options)
        positive_only = c5.toggle("Positive Kelly only", value=False)

    filtered = []
    for r in calculated:
        if selected_sports and r["sport_label"] not in selected_sports:
            continue
        if MARKET_LABELS.get(r["market"], r["market"]) not in selected_markets:
            continue
        if r["signal"] not in selected_signals:
            continue
        if BOOK_LABELS.get(r["book"], r["book"]) not in selected_books:
            continue
        if positive_only and r["full_kelly"] <= 0:
            continue
        filtered.append(r)

    priority = {"STRONG EDGE": 4, "EDGE": 3, "WATCH": 2, "PASS": 1}
    filtered.sort(key=lambda r: (priority.get(r["signal"], 0), r["ev"]), reverse=True)

    strong_count = sum(r["signal"] == "STRONG EDGE" for r in calculated)
    edge_count = sum(r["signal"] == "EDGE" for r in calculated)
    positive_count = sum(r["ev"] > 0 and r["full_kelly"] > 0 for r in calculated)
    top_ev = max((r["ev"] for r in calculated), default=None)

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("🔥 Strong edges", strong_count)
    m2.metric("🟢 Edges", edge_count)
    m3.metric("+Kelly prices", positive_count)
    m4.metric("Best current EV", f"{top_ev*100:.1f}%" if top_ev is not None else "—")

    tab1, tab2, tab3 = st.tabs(["📊 Live Board", "⚡ Signal History", "🧪 Diagnostics"])

    with tab1:
        if not filtered:
            st.info("No matched markets currently pass your selected filters. The board will update automatically.")
        else:
            display = []
            for r in filtered[:300]:
                point = "" if r.get("point") is None else f" {r['point']:+g}" if r["market"] == "spreads" else f" {r['point']:g}"
                flags = ", ".join(r["quality_flags"]) if r["quality_flags"] else "OK"
                display.append({
                    "Signal": r["signal"],
                    "Sport": r["sport_label"],
                    "Game": r["game"],
                    "Market": MARKET_LABELS.get(r["market"], r["market"]),
                    "Selection": f"{r['selection']}{point}",
                    "Book": BOOK_LABELS.get(r["book"], r["book"]),
                    "Poly %": r["poly_prob"] * 100,
                    "Odds": fmt_odds(r["odds"]),
                    "Break-even %": r["break_even"] * 100,
                    "No-vig %": (r["no_vig"] * 100 if r.get("no_vig") is not None else None),
                    "Edge pp": r["edge"] * 100,
                    "EV %": r["ev"] * 100,
                    f"{kelly_choice} %": r["fractional_kelly"] * 100,
                    "Stake $": r["stake"],
                    "Max price": fmt_odds(r["max_price"]),
                    "Poly spread": r.get("poly_spread"),
                    "Book age s": r.get("book_age"),
                    "Poly age s": r.get("poly_age"),
                    "Quality": flags,
                    "Match %": r.get("match_score"),
                })
            df = pd.DataFrame(display)
            st.dataframe(
                df,
                use_container_width=True,
                hide_index=True,
                height=min(760, 42 + len(df) * 35),
                column_config={
                    "Poly %": st.column_config.NumberColumn(format="%.2f%%"),
                    "Break-even %": st.column_config.NumberColumn(format="%.2f%%"),
                    "No-vig %": st.column_config.NumberColumn(format="%.2f%%"),
                    "Edge pp": st.column_config.NumberColumn(format="%+.2f"),
                    "EV %": st.column_config.NumberColumn(format="%+.2f%%"),
                    f"{kelly_choice} %": st.column_config.NumberColumn(format="%.2f%%"),
                    "Stake $": st.column_config.NumberColumn(format="$%.2f"),
                    "Poly spread": st.column_config.NumberColumn(format="%.3f"),
                    "Book age s": st.column_config.NumberColumn(format="%.0f"),
                    "Poly age s": st.column_config.NumberColumn(format="%.0f"),
                    "Match %": st.column_config.NumberColumn(format="%.0f%%"),
                },
            )

            st.caption("Edge = Polymarket fair probability − sportsbook raw break-even probability. No-vig is shown separately and is not used to calculate bet EV/Kelly.")

            with st.expander("Current strongest opportunities"):
                for r in filtered[:8]:
                    if r["signal"] == "PASS":
                        continue
                    st.markdown(
                        f"**{r['signal']} — {r['game']} — {r['selection']} ({BOOK_LABELS.get(r['book'], r['book'])} {fmt_odds(r['odds'])})**  \n"
                        f"Poly **{r['poly_prob']*100:.2f}%** • Edge **{r['edge']*100:+.2f} pp** • EV **{r['ev']*100:+.2f}%** • "
                        f"{kelly_choice} **{r['fractional_kelly']*100:.2f}%** (${r['stake']:.2f}) • Max acceptable **{fmt_odds(r['max_price'])}**"
                    )

    with tab2:
        hist = engine.snapshot()["signal_history"]
        if not hist:
            st.info("Signal transitions will appear here as the live board changes.")
        else:
            hdf = pd.DataFrame(hist)
            if not hdf.empty:
                hdf["edge"] = hdf["edge"] * 100
                hdf["ev"] = hdf["ev"] * 100
                st.dataframe(
                    hdf,
                    use_container_width=True,
                    hide_index=True,
                    column_config={
                        "edge": st.column_config.NumberColumn("Edge pp", format="%+.2f"),
                        "ev": st.column_config.NumberColumn("EV %", format="%+.2f%%"),
                    },
                )
                st.download_button(
                    "Download session signals CSV",
                    data=hdf.to_csv(index=False).encode("utf-8"),
                    file_name="drew_edge_signals.csv",
                    mime="text/csv",
                )

    with tab3:
        st.write({
            "Polymarket connection": conn.get("poly"),
            "Sportsbook connection": conn.get("sportsbook"),
            "Last Polymarket discovery": snap.get("last_poly_discovery"),
            "Last sportsbook poll": snap.get("last_book_poll"),
            "Polymarket markets": snap.get("poly_market_count"),
            "Polymarket quotes": snap.get("poly_quote_count"),
            "Sportsbook events": snap.get("sportsbook_event_count"),
            "Feed uptime minutes": round(snap.get("uptime", 0) / 60, 1),
        })
        errs = snap.get("errors", [])
        if errs:
            st.warning("Recent feed messages")
            st.dataframe(pd.DataFrame(errs), use_container_width=True, hide_index=True)
        else:
            st.success("No recent feed errors.")


live_board()

st.markdown("---")
st.caption(
    "Research/decision-support dashboard only. Polymarket prices are market estimates, not guaranteed true probabilities. "
    "Always verify the sportsbook price before wagering; provider latency, market rules, suspensions and liquidity can create false apparent edges."
)
