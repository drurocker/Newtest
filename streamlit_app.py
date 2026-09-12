
import json
import math
import os
import re
import time
from datetime import datetime, timezone
from difflib import SequenceMatcher
from typing import Any

import httpx
import pandas as pd
import streamlit as st

st.set_page_config(
    page_title="Drew Edge Board",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded",
)

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
ODDS_BASE = "https://api.the-odds-api.com/v4"

SPORTS = {
    "NFL": {"odds": "americanfootball_nfl", "poly": ["nfl"], "names": ["NFL"]},
    "NBA": {"odds": "basketball_nba", "poly": ["nba"], "names": ["NBA"]},
    "MLB": {"odds": "baseball_mlb", "poly": ["mlb"], "names": ["MLB"]},
    "NHL": {"odds": "icehockey_nhl", "poly": ["nhl"], "names": ["NHL"]},
    "UFC/MMA": {"odds": "mma_mixed_martial_arts", "poly": ["ufc", "mma"], "names": ["UFC", "MMA"]},
}
BOOKMAKERS = ["draftkings", "fanduel"]
BOOK_LABELS = {"draftkings": "DK", "fanduel": "FD"}
MARKET_TO_API = {"Moneyline": "h2h", "Spread": "spreads", "Total": "totals"}
API_TO_MARKET = {v: k for k, v in MARKET_TO_API.items()}


def get_secret(name: str, default: Any = "") -> Any:
    try:
        if name in st.secrets:
            return st.secrets[name]
    except Exception:
        pass
    return os.getenv(name, default)


def parse_jsonish(v, default=None):
    if v is None:
        return default
    if isinstance(v, (list, dict)):
        return v
    try:
        return json.loads(v)
    except Exception:
        return default


def parse_ts(v):
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


def norm(s: str) -> str:
    s = str(s or "").lower()
    repl = {
        "@": " ", " at ": " ", " vs. ": " ", " vs ": " ", "-": " ",
        ".": " ", "'": "", "&": " and ", "_": " ",
    }
    for a, b in repl.items():
        s = s.replace(a, b)
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def ratio(a: str, b: str) -> float:
    return 100.0 * SequenceMatcher(None, norm(a), norm(b)).ratio()


def token_set_ratio(a: str, b: str) -> float:
    ta, tb = set(norm(a).split()), set(norm(b).split())
    if not ta or not tb:
        return 0.0
    common = sorted(ta & tb)
    left = " ".join(common + sorted(ta - tb))
    right = " ".join(common + sorted(tb - ta))
    common_s = " ".join(common)
    scores = [100.0 * SequenceMatcher(None, left, right).ratio()]
    if common_s:
        scores += [
            100.0 * SequenceMatcher(None, common_s, left).ratio(),
            100.0 * SequenceMatcher(None, common_s, right).ratio(),
        ]
    return max(scores)


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


def metrics(p: float, odds: int, bankroll: float, kelly_fraction: float):
    dec = american_to_decimal(odds)
    b = dec - 1
    q = 1 - p
    be = 1 / dec
    ev = p * dec - 1
    full = max(0.0, (b * p - q) / b) if b > 0 else 0.0
    return {
        "break_even": be,
        "edge": p - be,
        "ev": ev,
        "full_kelly": full,
        "frac_kelly": full * kelly_fraction,
        "stake": bankroll * full * kelly_fraction,
        "max_price": american_from_prob(p),
    }


def fmt_odds(v):
    if v is None:
        return "—"
    try:
        v = int(v)
        return f"+{v}" if v > 0 else str(v)
    except Exception:
        return "—"


@st.cache_data(ttl=300, show_spinner=False)
def fetch_poly_sports_meta():
    with httpx.Client(timeout=20) as client:
        r = client.get(f"{GAMMA}/sports")
        r.raise_for_status()
        return r.json()


def pick_poly_sport_meta(all_meta, label):
    cfg = SPORTS[label]
    aliases = {x.lower() for x in cfg["poly"]}
    names = {x.lower() for x in cfg["names"]}
    candidates = []
    for item in all_meta:
        sport = str(item.get("sport") or "").lower()
        name = str(item.get("name") or "").lower()
        if sport in aliases or name in names:
            candidates.append(item)
    if not candidates and label == "UFC/MMA":
        for item in all_meta:
            t = f"{item.get('sport','')} {item.get('name','')}".lower()
            if "ufc" in t:
                candidates.append(item)
    return candidates


def classify_poly_market_type(v: Any):
    s = str(v or "").lower()
    if "moneyline" in s or s in {"ml", "winner"}:
        return "h2h"
    if "spread" in s:
        return "spreads"
    if "total" in s or "o/u" in s:
        return "totals"
    return None


@st.cache_data(ttl=120, show_spinner=False)
def fetch_poly_yes_markets(selected_sports_tuple):
    selected_sports = list(selected_sports_tuple)
    meta = fetch_poly_sports_meta()
    rows = []
    diag = {
        "events": 0,
        "markets_seen": 0,
        "yes_markets": 0,
        "non_yes_skipped": 0,
        "sports_meta": {},
        "errors": [],
    }
    with httpx.Client(timeout=25) as client:
        for label in selected_sports:
            sport_meta = pick_poly_sport_meta(meta, label)
            diag["sports_meta"][label] = [
                {
                    "sport": x.get("sport"),
                    "name": x.get("name"),
                    "tag": x.get("primaryTagId"),
                    "series": x.get("series"),
                }
                for x in sport_meta
            ]
            seen_event_ids = set()
            for sm in sport_meta[:3]:
                tag_id = sm.get("primaryTagId")
                series_raw = str(sm.get("series") or "").strip()
                query_variants = []
                if tag_id:
                    query_variants.append({"tag_id": tag_id})
                series_ids = [x.strip() for x in series_raw.split(",") if x.strip()]
                for sid in series_ids[:3]:
                    query_variants.append({"series_id": sid})
                for extra in query_variants[:4]:
                    try:
                        offset = 0
                        for _ in range(3):
                            params = {
                                "closed": "false",
                                "active": "true",
                                "limit": 100,
                                "offset": offset,
                                **extra,
                            }
                            r = client.get(f"{GAMMA}/events", params=params)
                            r.raise_for_status()
                            events = r.json()
                            if not isinstance(events, list):
                                break
                            if not events:
                                break
                            for ev in events:
                                ev_id = str(ev.get("id") or ev.get("slug") or "")
                                if ev_id in seen_event_ids:
                                    continue
                                seen_event_ids.add(ev_id)
                                diag["events"] += 1
                                ev_title = str(ev.get("title") or ev.get("ticker") or ev.get("slug") or "")
                                ev_start = parse_ts(
                                    ev.get("startTime")
                                    or ev.get("eventDate")
                                    or ev.get("startDate")
                                )
                                for m in ev.get("markets") or []:
                                    diag["markets_seen"] += 1
                                    mk_type = classify_poly_market_type(
                                        m.get("sportsMarketType")
                                        or m.get("sportsMarketTypeV2")
                                        or m.get("marketType")
                                    )
                                    if not mk_type:
                                        continue
                                    outcomes = parse_jsonish(m.get("outcomes"), []) or []
                                    tokens = parse_jsonish(m.get("clobTokenIds"), []) or []
                                    if not isinstance(outcomes, list) or not isinstance(tokens, list):
                                        continue
                                    if len(outcomes) != len(tokens) or not outcomes:
                                        continue
                                    yes_idx = next(
                                        (i for i, x in enumerate(outcomes) if norm(x) == "yes"),
                                        None,
                                    )
                                    if yes_idx is None:
                                        diag["non_yes_skipped"] += 1
                                        continue
                                    diag["yes_markets"] += 1
                                    line = m.get("line")
                                    if line is None:
                                        line = m.get("groupItemThreshold")
                                    question = str(
                                        m.get("question")
                                        or m.get("groupItemTitle")
                                        or ev_title
                                    )
                                    rows.append({
                                        "sport_label": label,
                                        "event_id": ev_id,
                                        "event_title": ev_title,
                                        "event_start": parse_ts(
                                            m.get("gameStartTime")
                                            or m.get("eventStartTime")
                                        ) or ev_start,
                                        "market_type": mk_type,
                                        "question": question,
                                        "group_title": str(m.get("groupItemTitle") or ""),
                                        "line": line,
                                        "token_id": str(tokens[yes_idx]),
                                        "market_id": str(m.get("id") or ""),
                                        "condition_id": m.get("conditionId"),
                                        "gamma_best_bid": m.get("bestBid"),
                                        "gamma_best_ask": m.get("bestAsk"),
                                        "gamma_last": m.get("lastTradePrice"),
                                        "liquidity": m.get("liquidityNum") or m.get("liquidity"),
                                        "volume": m.get("volumeNum") or m.get("volume"),
                                    })
                            if len(events) < 100:
                                break
                            offset += len(events)
                    except Exception as e:
                        diag["errors"].append(f"{label} Poly discovery: {e}")
    # Deduplicate identical token IDs
    unique = {}
    for r in rows:
        unique[r["token_id"]] = r
    return list(unique.values()), diag


@st.cache_data(ttl=60, show_spinner=False)
def fetch_sportsbook_events(api_key, selected_sports_tuple, selected_markets_tuple):
    events = []
    errors = []
    market_keys = [MARKET_TO_API[x] for x in selected_markets_tuple]
    if not api_key or not selected_sports_tuple or not market_keys:
        return events, errors
    with httpx.Client(timeout=25) as client:
        for label in selected_sports_tuple:
            sport_key = SPORTS[label]["odds"]
            params = {
                "apiKey": api_key,
                "bookmakers": ",".join(BOOKMAKERS),
                "markets": ",".join(market_keys),
                "oddsFormat": "american",
                "dateFormat": "iso",
            }
            try:
                r = client.get(f"{ODDS_BASE}/sports/{sport_key}/odds", params=params)
                if r.status_code == 422 and len(market_keys) > 1:
                    # Some sports expose only a subset of requested markets.
                    for one in market_keys:
                        p2 = dict(params)
                        p2["markets"] = one
                        rr = client.get(f"{ODDS_BASE}/sports/{sport_key}/odds", params=p2)
                        if rr.status_code == 200:
                            for ev in rr.json():
                                ev["_sport_label"] = label
                                events.append(ev)
                    continue
                r.raise_for_status()
                for ev in r.json():
                    ev["_sport_label"] = label
                    events.append(ev)
            except Exception as e:
                errors.append(f"{label} sportsbook: {e}")
    # Merge duplicate event IDs from fallback per-market calls
    merged = {}
    for ev in events:
        key = str(ev.get("id") or f"{ev.get('_sport_label')}|{ev.get('away_team')}|{ev.get('home_team')}|{ev.get('commence_time')}")
        if key not in merged:
            merged[key] = ev
            continue
        old = merged[key]
        old_books = {b.get("key"): b for b in old.get("bookmakers", [])}
        for b in ev.get("bookmakers", []):
            bk = b.get("key")
            if bk not in old_books:
                old.setdefault("bookmakers", []).append(b)
            else:
                target = old_books[bk]
                have = {m.get("key") for m in target.get("markets", [])}
                for m in b.get("markets", []):
                    if m.get("key") not in have:
                        target.setdefault("markets", []).append(m)
    return list(merged.values()), errors


def event_score(poly, book_event):
    ptitle = poly["event_title"]
    home = str(book_event.get("home_team") or "")
    away = str(book_event.get("away_team") or "")
    hs = token_set_ratio(ptitle, home)
    aws = token_set_ratio(ptitle, away)
    both = token_set_ratio(ptitle, f"{away} {home}")
    # Require evidence for both teams where possible; reward the combined phrase.
    return 0.35 * hs + 0.35 * aws + 0.30 * both


def numbers_in_text(s):
    return [float(x) for x in re.findall(r"(?<!\d)(\d+(?:\.\d+)?)", str(s or ""))]


def line_compatible(poly, book_market, outcome):
    point = outcome.get("point")
    if point is None:
        return True
    try:
        point = float(point)
    except Exception:
        return True
    try:
        if poly.get("line") is not None:
            return abs(abs(float(poly["line"])) - abs(point)) <= 0.26
    except Exception:
        pass
    nums = numbers_in_text(poly.get("question", "") + " " + poly.get("group_title", ""))
    return any(abs(abs(x) - abs(point)) <= 0.26 for x in nums) if nums else True


def selection_score(poly, book_market, outcome):
    question = f"{poly.get('question','')} {poly.get('group_title','')}"
    name = str(outcome.get("name") or "")
    if book_market == "h2h":
        return token_set_ratio(question, name)
    if book_market == "spreads":
        if not line_compatible(poly, book_market, outcome):
            return 0.0
        return token_set_ratio(question, name)
    if book_market == "totals":
        if not line_compatible(poly, book_market, outcome):
            return 0.0
        side = norm(name)
        q = norm(question)
        if side == "over":
            return 100.0 if "over" in q else 0.0
        if side == "under":
            # YES-only means a Poly question phrased as "over" cannot be used
            # as the sportsbook Under price by simply taking the NO token.
            return 100.0 if "under" in q else 0.0
        return token_set_ratio(question, name)
    return 0.0


def match_metadata(poly_markets, book_events, selected_markets):
    wanted = {MARKET_TO_API[x] for x in selected_markets}
    by_sport_type = {}
    for p in poly_markets:
        by_sport_type.setdefault((p["sport_label"], p["market_type"]), []).append(p)

    matched = []
    attempts = 0
    for ev in book_events:
        sport = ev.get("_sport_label")
        book_start = parse_ts(ev.get("commence_time"))
        for bk in ev.get("bookmakers", []):
            book_key = bk.get("key")
            if book_key not in BOOKMAKERS:
                continue
            book_age = max(0.0, time.time() - (parse_ts(bk.get("last_update")) or time.time()))
            for mk in bk.get("markets", []):
                mkt = mk.get("key")
                if mkt not in wanted:
                    continue
                outs = mk.get("outcomes") or []
                implieds = [implied_prob(float(o["price"])) for o in outs if o.get("price") is not None]
                denom = sum(implieds)
                for out in outs:
                    if out.get("price") is None:
                        continue
                    attempts += 1
                    best = None
                    for p in by_sport_type.get((sport, mkt), []):
                        pstart = p.get("event_start")
                        if pstart and book_start and abs(pstart - book_start) > 12 * 3600:
                            continue
                        es = event_score(p, ev)
                        if es < 52:
                            continue
                        ss = selection_score(p, mkt, out)
                        if ss < 58:
                            continue
                        score = 0.58 * es + 0.42 * ss
                        if best is None or score > best[0]:
                            best = (score, p)
                    if not best:
                        continue
                    score, p = best
                    matched.append({
                        "sport": sport,
                        "game": f"{ev.get('away_team')} @ {ev.get('home_team')}",
                        "start": ev.get("commence_time"),
                        "market": mkt,
                        "selection": out.get("name"),
                        "point": out.get("point"),
                        "book": book_key,
                        "odds": int(out["price"]),
                        "no_vig": implied_prob(float(out["price"])) / denom if denom else None,
                        "book_age": book_age,
                        "match_score": score,
                        "token_id": p["token_id"],
                        "poly_question": p["question"],
                        "poly_event": p["event_title"],
                        "poly_line": p.get("line"),
                        "poly_liquidity": p.get("liquidity"),
                        "poly_volume": p.get("volume"),
                    })
    return matched, attempts


def fetch_clob_books(token_ids):
    token_ids = list(dict.fromkeys([str(x) for x in token_ids if x]))
    result = {}
    errors = []
    if not token_ids:
        return result, errors
    with httpx.Client(timeout=20) as client:
        for i in range(0, len(token_ids), 100):
            batch = token_ids[i:i+100]
            try:
                r = client.post(f"{CLOB}/books", json=[{"token_id": t} for t in batch])
                r.raise_for_status()
                for b in r.json():
                    tok = str(b.get("asset_id") or "")
                    bids = b.get("bids") or []
                    asks = b.get("asks") or []
                    bid = max((float(x.get("price")) for x in bids if x.get("price") is not None), default=None)
                    ask = min((float(x.get("price")) for x in asks if x.get("price") is not None), default=None)
                    depth_bid = sum(float(x.get("size") or 0) for x in bids[:5])
                    depth_ask = sum(float(x.get("size") or 0) for x in asks[:5])
                    result[tok] = {
                        "bid": bid,
                        "ask": ask,
                        "spread": (ask - bid) if bid is not None and ask is not None else None,
                        "mid": ((bid + ask) / 2) if bid is not None and ask is not None and ask >= bid else None,
                        "last": float(b.get("last_trade_price")) if b.get("last_trade_price") not in (None, "") else None,
                        "ts": parse_ts(b.get("timestamp")) or time.time(),
                        "depth_bid": depth_bid,
                        "depth_ask": depth_ask,
                    }
            except Exception as e:
                errors.append(f"CLOB books batch: {e}")
    return result, errors


def signal(row, bankroll, kelly_fraction, min_ev, strong_ev, strong_edge_pp, max_spread, max_book_age, max_poly_age):
    m = metrics(row["poly_prob"], row["odds"], bankroll, kelly_fraction)
    flags = []
    if row.get("poly_spread") is not None and row["poly_spread"] > max_spread:
        flags.append("WIDE POLY SPREAD")
    if row.get("book_age", 0) > max_book_age:
        flags.append("STALE BOOK")
    if row.get("poly_age", 0) > max_poly_age:
        flags.append("STALE POLY")
    if row.get("match_score", 0) < 65:
        flags.append("LOW MATCH")
    if flags or m["ev"] <= 0 or m["full_kelly"] <= 0:
        sig = "PASS"
    elif m["ev"] < min_ev:
        sig = "WATCH"
    elif m["ev"] >= strong_ev and m["edge"] * 100 >= strong_edge_pp:
        sig = "STRONG EDGE"
    else:
        sig = "EDGE"
    return {**row, **m, "signal": sig, "flags": flags}


st.markdown("""
<style>
.block-container {padding-top: 1rem; max-width: 1600px;}
.status {padding:.6rem .8rem;border:1px solid rgba(128,128,128,.3);border-radius:.7rem;margin:.4rem 0 .8rem;}
.small {color:#8b98a9;font-size:.9rem}
</style>
""", unsafe_allow_html=True)

st.title("DREW EDGE BOARD")
st.caption("Polymarket YES × DraftKings × FanDuel • Edge • EV • Kelly")

secret_key = str(get_secret("ODDS_API_KEY", "")).strip()
with st.sidebar:
    st.header("⚙️ Feed & Edge Settings")
    if secret_key:
        api_key = secret_key
        st.success("Odds API key loaded from Streamlit Secrets.")
    else:
        api_key = st.text_input("The Odds API key", type="password", placeholder="Paste for this session").strip()

    selected_sports = st.multiselect(
        "Sports to monitor",
        list(SPORTS.keys()),
        default=["NFL"],
        help="Monitoring fewer sports saves The Odds API credits.",
    )
    selected_markets = st.multiselect(
        "Markets to monitor",
        list(MARKET_TO_API.keys()),
        default=["Moneyline"],
        help="Start with Moneyline to verify matching, then add spreads/totals.",
    )
    bankroll = st.number_input("Bankroll ($)", min_value=1.0, value=float(get_secret("BANKROLL", 1000)), step=50.0)
    kelly_label = st.selectbox("Kelly sizing", ["1/8 Kelly", "1/4 Kelly", "1/2 Kelly", "Full Kelly"], index=1)
    kelly_fraction = {"1/8 Kelly": .125, "1/4 Kelly": .25, "1/2 Kelly": .5, "Full Kelly": 1.0}[kelly_label]
    min_ev_pct = st.slider("Minimum EV for EDGE", 0.0, 10.0, 3.0, .5)
    strong_ev_pct = st.slider("STRONG EDGE EV", 1.0, 15.0, 5.0, .5)
    strong_edge_pp = st.slider("STRONG EDGE probability edge (pp)", .5, 8.0, 2.0, .5)
    max_spread = st.slider("Max Poly spread", .01, .15, .05, .01)
    max_book_age = st.slider("Max sportsbook quote age (sec)", 30, 240, 95, 5)
    max_poly_age = st.slider("Max Poly quote age (sec)", 5, 120, 30, 5)
    if api_key:
        st.caption("Sportsbook data cached for 60s to protect API credits.")
    else:
        st.warning("Add your Odds API key to load DK/FD.")

if not selected_sports:
    st.info("Choose at least one sport in the sidebar.")
    st.stop()
if not selected_markets:
    st.info("Choose at least one market in the sidebar.")
    st.stop()

@st.fragment(run_every="5s")
def board():
    poly_markets, poly_diag = fetch_poly_yes_markets(tuple(selected_sports))
    book_events, book_errors = fetch_sportsbook_events(api_key, tuple(selected_sports), tuple(selected_markets))
    meta_matches, attempts = match_metadata(poly_markets, book_events, selected_markets)
    books, clob_errors = fetch_clob_books([x["token_id"] for x in meta_matches])

    rows = []
    now = time.time()
    for x in meta_matches:
        q = books.get(x["token_id"])
        if not q:
            continue
        p = q.get("mid")
        if p is None:
            p = q.get("last")
        if p is None or not (0 < p < 1):
            continue
        row = {
            **x,
            "poly_prob": p,
            "poly_bid": q.get("bid"),
            "poly_ask": q.get("ask"),
            "poly_spread": q.get("spread"),
            "poly_age": max(0.0, now - (q.get("ts") or now)),
            "poly_depth_bid": q.get("depth_bid"),
            "poly_depth_ask": q.get("depth_ask"),
        }
        rows.append(signal(
            row,
            bankroll,
            kelly_fraction,
            min_ev_pct / 100,
            strong_ev_pct / 100,
            strong_edge_pp,
            max_spread,
            max_book_age,
            max_poly_age,
        ))

    poly_ok = len(poly_markets) > 0
    book_ok = bool(book_events) if api_key else False
    st.markdown(
        f'<div class="status">'
        f'Polymarket sports: <b>{"🟢 LIVE" if poly_ok else "🟡 WAITING"}</b> • '
        f'Sportsbooks: <b>{"🟢 LIVE" if book_ok else ("🟡 NO KEY" if not api_key else "🟡 WAITING")}</b> • '
        f'Poly YES markets: <b>{len(poly_markets)}</b> • '
        f'Book events: <b>{len(book_events)}</b> • '
        f'Metadata matches: <b>{len(meta_matches)}</b> • '
        f'Live priced matches: <b>{len(rows)}</b>'
        f'</div>',
        unsafe_allow_html=True,
    )

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("🔥 Strong edges", sum(r["signal"] == "STRONG EDGE" for r in rows))
    c2.metric("🟢 Edges", sum(r["signal"] == "EDGE" for r in rows))
    c3.metric("+Kelly prices", sum(r["full_kelly"] > 0 for r in rows))
    top_ev = max((r["ev"] for r in rows), default=None)
    c4.metric("Best EV", f"{top_ev*100:+.1f}%" if top_ev is not None else "—")

    tabs = st.tabs(["📊 Live Board", "🧪 Diagnostics"])

    with tabs[0]:
        signal_filter = st.multiselect(
            "Signals",
            ["STRONG EDGE", "EDGE", "WATCH", "PASS"],
            default=["STRONG EDGE", "EDGE", "WATCH"],
            key="signal_filter_v4",
        )
        book_filter = st.multiselect("Books", ["DK", "FD"], default=["DK", "FD"], key="book_filter_v4")
        positive_only = st.toggle("Positive Kelly only", value=False, key="positive_only_v4")

        filtered = []
        for r in rows:
            if r["signal"] not in signal_filter:
                continue
            if BOOK_LABELS.get(r["book"], r["book"]) not in book_filter:
                continue
            if positive_only and r["full_kelly"] <= 0:
                continue
            filtered.append(r)
        priority = {"STRONG EDGE": 4, "EDGE": 3, "WATCH": 2, "PASS": 1}
        filtered.sort(key=lambda r: (priority.get(r["signal"], 0), r["ev"]), reverse=True)

        if not rows:
            if not api_key:
                st.info("Enter your Odds API key in the sidebar.")
            elif not poly_markets:
                st.warning("No Polymarket YES sports markets were discovered. Open Diagnostics.")
            elif not book_events:
                st.warning("No DraftKings/FanDuel events were returned. Open Diagnostics.")
            elif not meta_matches:
                st.warning("Both feeds are loaded, but the event/market matcher found no same-market pairs yet. Open Diagnostics for sample data.")
            else:
                st.warning("Markets matched, but no current Polymarket order book prices were available.")
        elif not filtered:
            st.info("Matched prices exist, but your current signal filters hide them. Add PASS or turn off Positive Kelly only.")
        else:
            display = []
            for r in filtered[:300]:
                point = ""
                if r.get("point") is not None:
                    try:
                        point = f" {float(r['point']):+g}" if r["market"] == "spreads" else f" {float(r['point']):g}"
                    except Exception:
                        pass
                display.append({
                    "Signal": r["signal"],
                    "Sport": r["sport"],
                    "Game": r["game"],
                    "Market": API_TO_MARKET.get(r["market"], r["market"]),
                    "Selection": f"{r['selection']}{point}",
                    "Book": BOOK_LABELS.get(r["book"], r["book"]),
                    "Poly YES %": r["poly_prob"] * 100,
                    "DK/FD Odds": fmt_odds(r["odds"]),
                    "Break-even %": r["break_even"] * 100,
                    "No-vig %": r["no_vig"] * 100 if r.get("no_vig") is not None else None,
                    "Edge pp": r["edge"] * 100,
                    "EV %": r["ev"] * 100,
                    f"{kelly_label} %": r["frac_kelly"] * 100,
                    "Stake $": r["stake"],
                    "Max price": fmt_odds(r["max_price"]),
                    "Poly spread": r.get("poly_spread"),
                    "Book age s": r.get("book_age"),
                    "Poly age s": r.get("poly_age"),
                    "Match %": r.get("match_score"),
                    "Quality": ", ".join(r["flags"]) if r["flags"] else "OK",
                    "Poly question": r.get("poly_question"),
                })
            df = pd.DataFrame(display)
            st.dataframe(
                df,
                use_container_width=True,
                hide_index=True,
                height=min(780, 45 + 35 * len(df)),
                column_config={
                    "Poly YES %": st.column_config.NumberColumn(format="%.2f%%"),
                    "Break-even %": st.column_config.NumberColumn(format="%.2f%%"),
                    "No-vig %": st.column_config.NumberColumn(format="%.2f%%"),
                    "Edge pp": st.column_config.NumberColumn(format="%+.2f"),
                    "EV %": st.column_config.NumberColumn(format="%+.2f%%"),
                    f"{kelly_label} %": st.column_config.NumberColumn(format="%.2f%%"),
                    "Stake $": st.column_config.NumberColumn(format="$%.2f"),
                    "Poly spread": st.column_config.NumberColumn(format="%.3f"),
                    "Match %": st.column_config.NumberColumn(format="%.1f"),
                },
            )
            st.caption("Only Polymarket YES tokens are used. NO tokens are never converted into opposing sportsbook selections.")

    with tabs[1]:
        st.write({
            "Selected sports": selected_sports,
            "Selected markets": selected_markets,
            "Polymarket events scanned": poly_diag.get("events"),
            "Polymarket markets scanned": poly_diag.get("markets_seen"),
            "Polymarket YES markets": poly_diag.get("yes_markets"),
            "Polymarket non-YES markets skipped": poly_diag.get("non_yes_skipped"),
            "Sportsbook events": len(book_events),
            "Sportsbook outcome attempts": attempts,
            "Metadata matches": len(meta_matches),
            "CLOB priced matches": len(rows),
        })
        if poly_diag.get("sports_meta"):
            with st.expander("Polymarket sport metadata used"):
                st.json(poly_diag["sports_meta"])
        if poly_markets:
            with st.expander("Sample Polymarket YES markets"):
                st.dataframe(pd.DataFrame(poly_markets[:20])[
                    ["sport_label", "event_title", "market_type", "question", "line", "token_id"]
                ], use_container_width=True, hide_index=True)
        if book_events:
            with st.expander("Sample sportsbook events"):
                sample = [{
                    "sport": e.get("_sport_label"),
                    "away": e.get("away_team"),
                    "home": e.get("home_team"),
                    "start": e.get("commence_time"),
                    "books": ", ".join(b.get("key","") for b in e.get("bookmakers", [])),
                } for e in book_events[:20]]
                st.dataframe(pd.DataFrame(sample), use_container_width=True, hide_index=True)
        if meta_matches:
            with st.expander("Sample metadata matches"):
                st.dataframe(pd.DataFrame(meta_matches[:30])[
                    ["sport", "game", "market", "selection", "book", "odds", "match_score", "poly_question", "token_id"]
                ], use_container_width=True, hide_index=True)
        errors = (poly_diag.get("errors") or []) + book_errors + clob_errors
        if errors:
            st.error("\n\n".join(errors[:10]))

board()

st.divider()
st.caption(
    "Research/decision-support dashboard only. Polymarket prices are market estimates, not guaranteed true probabilities. "
    "Verify the sportsbook price and market rules before wagering."
)
