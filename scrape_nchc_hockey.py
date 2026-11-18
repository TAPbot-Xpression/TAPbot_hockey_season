#!/usr/bin/env python3
"""
scrape_nchc_hockey.py
NCHC Men's Hockey player season stats + team stats scraper (Sidearm sites).

Fixes applied:
1. Accurate PPG, SHG, and GWG Extraction:
   - Nuxt resolver aggregates all cumulative situation goalStats (e.g. powerPlayGoals: 4, shortHandedGoals: 1 for Lucas Van Vliet).
   - HTML table parser accurately maps [GP, G, A, PTS, SH, SH%, +/-, PPG, SHG, GWG, BLK, PN-PIM] without column drift.
2. Direct Skater & Goalie Property Mapping:
   - Skaters: gp, g, a, pts, sh, blk, ppg, shg, gwg, pn_pim
   - Goalies: gp_gs, ga, gaa, sv, sv_pct, w_l_t, min
3. Clean Roster Integration:
   - Roster gate ensures only true players are output, discarding game-by-game results, totals, and opponent lines.
4. Correct Section-Scoped Team Stats:
   - Power Play%: Home conversion rate (e.g. 22.2%)
   - Penalty Kill%: Opponent conversion inverted (1.0 - .235 = 76.5%)
   - Penalties / G: Home rate (e.g. 4.6)
   - PIM / G: Home rate (e.g. 12.5)
   - Faceoff%: Home rate (e.g. 52.0%)
5. Non-Conference Support: Auto-loads non_conf_config.json if present.
"""

from __future__ import annotations

import argparse
import asyncio
import difflib
import json
import logging
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from bs4 import BeautifulSoup
from playwright.async_api import BrowserContext, Page, async_playwright

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

CONCURRENCY_LIMIT = 5
MIN_ROSTER_ENTRIES = 12
MIN_PLAYERS_ACCEPT = 5

TEAMS_CONFIG = [
    {
        "name": "St. Thomas", "slug": "st_thomas_mhockey", "style": "new",
        "stats_url": "https://tommiesports.com/sports/mhockey/stats/2026-27",
        "roster_url": "https://tommiesports.com/sports/mhockey/roster/print",
    },
    {
        "name": "Colorado College", "slug": "colorado_college_mhockey", "style": "old",
        "stats_url": "https://cctigers.com/sports/mens-ice-hockey/stats/2026-2027",
        "roster_url": "https://cctigers.com/sports/mens-ice-hockey/roster?print=true",
    },
    {
        "name": "North Dakota", "slug": "north_dakota_mhockey", "style": "old",
        "stats_url": "https://fightinghawks.com/sports/mens-ice-hockey/stats",
        "roster_url": "https://fightinghawks.com/sports/mens-ice-hockey/roster?print=true",
    },
    {
        "name": "Arizona State", "slug": "arizona_state_mhockey", "style": "asu",
        "stats_url": "https://thesundevils.com/sports/ice-hockey/stats",
        "roster_url": "https://thesundevils.com/sports/ice-hockey/roster?view=list",
    },
    {
        "name": "Denver", "slug": "denver_mhockey", "style": "new",
        "stats_url": "https://denverpioneers.com/sports/mens-ice-hockey/stats/2026-27",
        "roster_url": "https://denverpioneers.com/sports/mens-ice-hockey/roster/print",
    },
    {
        "name": "Minnesota-Duluth", "slug": "minnesota_duluth_mhockey", "style": "old",
        "stats_url": "https://umdbulldogs.com/sports/mens-ice-hockey/stats",
        "roster_url": "https://umdbulldogs.com/sports/mens-ice-hockey/roster?print=true",
    },
    {
        "name": "St. Cloud State", "slug": "st_cloud_state_mhockey", "style": "new",
        "stats_url": "https://scsuhuskies.com/sports/mens-ice-hockey/stats/2026-27",
        "roster_url": "https://scsuhuskies.com/sports/mens-ice-hockey/roster/print",
    },
    {
        "name": "Western Michigan", "slug": "western_michigan_mhockey", "style": "old",
        "stats_url": "https://wmubroncos.com/sports/mens-ice-hockey/stats",
        "roster_url": "https://wmubroncos.com/sports/mens-ice-hockey/roster?print=true",
    },
    {
        "name": "Miami (OH)", "slug": "miami_oh_mhockey", "style": "new",
        "stats_url": "https://miamiredhawks.com/sports/mens-ice-hockey/stats/2026-27",
        "roster_url": "https://miamiredhawks.com/sports/mens-ice-hockey/roster/print",
    },
    {
        "name": "Omaha", "slug": "omaha_mhockey", "style": "new",
        "stats_url": "https://omahamavs.com/sports/mens-ice-hockey/stats/2026-27",
        "roster_url": "https://omahamavs.com/sports/mens-ice-hockey/roster/print",
    },
]

SKATER_STAT_FIELDS = ["gp", "g", "a", "pts", "sh", "blk", "ppg", "shg", "gwg", "pn_pim"]
GOALIE_STAT_FIELDS = ["gp_gs", "ga", "gaa", "sv", "sv_pct", "w_l_t", "min"]
TEAM_STAT_KEYS = [
    "goals_per_game", "shots_per_game", "shot_pct", "pp_pct",
    "pk_pct", "pen_per_game", "pim_per_game", "faceoff_pct",
]

DISCARD_TERMS = {
    "team", "tm", "total", "totals", "opponent", "opponents", "opp", "season", "overall",
    "conference", "goals", "assists", "points", "shots", "penalties", "penalty minutes",
    "power play goals", "saves", "unassigned", "scoring", "averages", "minutes", "view bio"
}

_POS_MAP = {
    "G": "G", "GK": "G", "GOALIE": "G", "GOALTENDER": "G", "GOALKEEPER": "G", "GOALTENDERS": "G",
    "D": "D", "DEF": "D", "DEFENSE": "D", "DEFENSEMAN": "D", "DEFENSEMEN": "D", "DEFENCE": "D",
    "DEFENCEMAN": "D", "DMAN": "D", "LD": "D", "RD": "D",
    "F": "F", "FWD": "F", "FORWARD": "F", "C": "F", "CENTER": "F", "CENTRE": "F",
    "LW": "F", "RW": "F", "W": "F", "WING": "F", "WINGER": "F", "LEFTWING": "F", "RIGHTWING": "F",
}

_CLASS_MAP = {
    "FR": "FR", "FRESHMAN": "FR", "FY": "FR", "FIRST": "FR", "1ST": "FR",
    "SO": "SO", "SOPHOMORE": "SO", "SECOND": "SO", "2ND": "SO",
    "JR": "JR", "JUNIOR": "JR", "THIRD": "JR", "3RD": "JR",
    "SR": "SR", "SENIOR": "SR", "FOURTH": "SR", "4TH": "SR",
    "GR": "GR", "GRAD": "GR", "GRADUATE": "GR", "GS": "GR", "FIFTH": "GR", "5TH": "GR",
}

def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

def normalize_position(pos_str: str) -> str:
    if not pos_str:
        return ""
    p = re.sub(r"[^A-Z/]", "", str(pos_str).upper())
    for token in p.split("/"):
        if token in _POS_MAP:
            return _POS_MAP[token]
    return ""

def normalize_year(yr_str: str) -> str:
    if not yr_str:
        return ""
    y = re.sub(r"[^A-Z0-9]", "", str(yr_str).upper())
    if y in _CLASS_MAP:
        return _CLASS_MAP[y]
    for prefix in ("REDSHIRT", "RS", "R"):
        if y.startswith(prefix):
            rem = y[len(prefix):]
            if rem in _CLASS_MAP:
                base = _CLASS_MAP[rem]
                return f"R-{base}" if base != "GR" else "GR"
    return ""

def clean_name(raw_name: str) -> str:
    if not raw_name:
        return ""
    text = raw_name.replace("View Bio", "").replace("Image", "").strip()
    text = re.sub(r"['\"]+", "", text).strip()
    text = re.sub(r"#\d{1,2}\b", " ", text)
    text = re.sub(r"#?tm\b", " ", text, flags=re.I)
    text = text.strip().rstrip(",").strip()
    if "," in text:
        parts = [p.strip() for p in text.split(",") if p.strip()]
        last = parts[0]
        first = " ".join(parts[1:])
        text = f"{first} {last}"
    return " ".join(text.split()).strip()

def clean_number(raw_num: Any) -> str:
    if raw_num is None:
        return ""
    text = str(raw_num).strip().replace("#", "")
    m = re.match(r"^(\d{1,2})$", text)
    return str(int(m.group(1))) if m else ""

def clean_stat_int(val: Any) -> str:
    if val is None:
        return "0"
    s = str(val).strip()
    if not s or s in ("-", "--", "—", "null", "None", "N/A", "na"):
        return "0"
    try:
        return str(int(round(float(s))))
    except Exception:
        m = re.match(r"^(\d+)", s)
        return m.group(1) if m else "0"

def clean_stat_pct(val: Any) -> str:
    if val is None or val == "":
        return ".000"
    s = str(val).strip()
    try:
        f = float(s.rstrip("%"))
        if f > 1.0:
            f = f / 100.0
        return f"{f:.3f}".lstrip("0") if f < 1.0 else f"{f:.3f}"
    except Exception:
        return s

def clean_pn_pim(val: Any) -> str:
    if val is None:
        return "0-0"
    s = str(val).strip().replace("–", "-").replace("—", "-")
    m = re.match(r"^(\d+)\s*[-/]\s*(\d+)$", s)
    if m:
        return f"{int(m.group(1))}-{int(m.group(2))}"
    if re.match(r"^\d+$", s):
        return f"0-{int(s)}"
    return "0-0"

def _f(x: Any) -> Optional[float]:
    if x in (None, ""):
        return None
    s = str(x).strip().replace("−", "-").replace("–", "-").replace(",", "").rstrip("%")
    try:
        return float(s)
    except Exception:
        return None

def is_valid_player_candidate(name: str) -> bool:
    if not name or len(name) < 3:
        return False
    low = name.lower().strip()
    if low in DISCARD_TERMS:
        return False
    if low.startswith("at ") or low.startswith("vs ") or low.startswith("#"):
        return False
    if re.match(r"^[\d\.\-\s/%:]+$", low):
        return False
    return True

# ── Non-Conference Config Loader ─────────────────────────────────────────────
def load_non_conf_config() -> List[Dict[str, Any]]:
    path = "non_conf_config.json"
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        teams = []
        if isinstance(cfg, dict):
            for key, info in cfg.items():
                if isinstance(info, dict) and info.get("stats_url"):
                    name = info.get("name") or "Non-Conf Opponent"
                    slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "non_conf_mhockey"
                    teams.append({
                        "name": name,
                        "slug": slug,
                        "style": "new",
                        "stats_url": info["stats_url"],
                        "roster_url": info.get("roster_url") or "",
                    })
        return teams
    except Exception as e:
        logger.warning(f"Could not load non_conf_config.json: {e}")
        return []

# ── Browser Page Fetcher ─────────────────────────────────────────────────────
async def fetch_page_html(context: BrowserContext, url: str, wait_for_table: bool = False) -> Optional[str]:
    page: Optional[Page] = None
    try:
        page = await context.new_page()
        logger.info(f"Navigating to {url}")
        await page.goto(url, wait_until="domcontentloaded", timeout=25000)
        try:
            await page.wait_for_load_state("networkidle", timeout=3000)
        except Exception:
            pass
        if wait_for_table:
            try:
                await page.wait_for_selector("table", timeout=6000)
            except Exception:
                pass
        return await page.content()
    except Exception as e:
        logger.error(f"Failed to fetch {url}: {e}")
        return None
    finally:
        if page:
            await page.close()

# ── Roster Resolution Helpers ────────────────────────────────────────────────
def _add_roster(lookup: Dict[str, Dict[str, str]], name: str, num: str, pos: str, yr: str) -> None:
    info = {"name": name, "number": num, "position": pos, "year": yr}
    lookup[name.lower()] = info
    if num != "":
        lookup[f"#{num}"] = info

def find_roster_entry(name: str, number: str, roster_lookup: Dict[str, Dict[str, str]]) -> Optional[Dict[str, str]]:
    if not roster_lookup:
        return None
    raw_low = name.lower().strip()
    if raw_low in roster_lookup:
        return roster_lookup[raw_low]
    if number and f"#{number}" in roster_lookup:
        return roster_lookup[f"#{number}"]
    close = difflib.get_close_matches(raw_low, [k for k in roster_lookup if not k.startswith("#")], n=1, cutoff=0.72)
    return roster_lookup[close[0]] if close else None

NUXT_PATTERNS = [
    r'<script[^>]+id=["\']__NUXT_DATA__["\'][^>]*>(.*?)</script>',
    r"__NUXT_DATA__\s*=\s*(\[.*?\]);",
    r"window\.__NUXT__\s*=\s*(.*?);\s*</script>",
]

def deep_resolve_nuxt(val: Any, data: List[Any], depth: int = 0) -> Any:
    if depth > 10:
        return val
    if isinstance(val, int) and 0 <= val < len(data):
        target = data[val]
        return deep_resolve_nuxt(target, data, depth + 1)
    elif isinstance(val, dict):
        return {k: deep_resolve_nuxt(v, data, depth + 1) for k, v in val.items()}
    elif isinstance(val, list):
        return [deep_resolve_nuxt(v, data, depth + 1) for v in val]
    return val

def parse_nuxt_roster_data(html: str) -> Dict[str, Dict[str, str]]:
    roster_lookup: Dict[str, Dict[str, str]] = {}
    for pat in NUXT_PATTERNS:
        m = re.search(pat, html, re.DOTALL)
        if not m:
            continue
        try:
            data = json.loads(m.group(1))
            if not isinstance(data, list):
                continue

            for item in data:
                if not isinstance(item, dict):
                    continue
                resolved = deep_resolve_nuxt(item, data)
                if not isinstance(resolved, dict):
                    continue

                raw_name = (resolved.get("name") or resolved.get("playerName") or resolved.get("fullName") or resolved.get("displayName"))
                if not raw_name or not isinstance(raw_name, str):
                    continue
                c_name = clean_name(raw_name)
                if not is_valid_player_candidate(c_name):
                    continue

                raw_num = resolved.get("uniform") or resolved.get("jersey") or resolved.get("number") or resolved.get("jerseyNumber") or resolved.get("uniformNumber")
                raw_pos = resolved.get("position") or resolved.get("positionShort") or resolved.get("pos")
                raw_yr = resolved.get("academicYear") or resolved.get("year") or resolved.get("class") or resolved.get("cl")

                _add_roster(roster_lookup, c_name, clean_number(raw_num),
                            normalize_position(str(raw_pos or "")), normalize_year(str(raw_yr or "")))
        except Exception:
            continue
        if len(roster_lookup) >= 5:
            break
    return roster_lookup

def parse_print_roster(html: str) -> Dict[str, Dict[str, str]]:
    roster_lookup = parse_nuxt_roster_data(html)
    if len(roster_lookup) >= 5:
        return roster_lookup
    soup = BeautifulSoup(html, "html.parser")
    for row in soup.find_all("tr"):
        cells = [c.get_text(strip=True) for c in row.find_all(["td", "th"])]
        if len(cells) < 3:
            continue
        num, pos, yr, name_cand = "", "", "", ""
        for col in cells:
            if not num and re.match(r"^\d{1,2}$", col):
                num = clean_number(col)
                continue
            if not pos and normalize_position(col):
                pos = normalize_position(col)
                continue
            if not yr and normalize_year(col):
                yr = normalize_year(col)
                continue
            c_clean = clean_name(col)
            if not name_cand and is_valid_player_candidate(c_clean):
                name_cand = c_clean
        if name_cand:
            _add_roster(roster_lookup, name_cand, num, pos, yr)
    return roster_lookup

async def fetch_roster(context: BrowserContext, candidates: List[str], label: str) -> Tuple[Dict[str, Dict[str, str]], str]:
    best_lookup: Dict[str, Dict[str, str]] = {}
    best_url = ""
    for url in candidates:
        if not url:
            continue
        html = await fetch_page_html(context, url)
        if not html:
            continue
        lookup = parse_print_roster(html)
        if len(lookup) > len(best_lookup):
            best_lookup, best_url = lookup, url
        if len(lookup) >= MIN_ROSTER_ENTRIES:
            return lookup, url
    return best_lookup, best_url

# ── Nuxt Data Player Stats Extractor ──────────────────────────────────────────
def parse_nuxt_data_stats(html: str) -> List[Dict[str, Any]]:
    players: List[Dict[str, Any]] = []

    for pat in NUXT_PATTERNS:
        m = re.search(pat, html, re.DOTALL)
        if not m:
            continue
        try:
            data = json.loads(m.group(1))
            if not isinstance(data, list):
                continue

            for raw_item in data:
                if not isinstance(raw_item, dict):
                    continue

                item = deep_resolve_nuxt(raw_item, data)
                if not isinstance(item, dict):
                    continue

                if any(k in item for k in ("boxscoreUrl", "opponent", "winOrLoss", "gameDate")):
                    continue

                flat: Dict[str, Any] = {}
                def extract_flat(d: Dict[str, Any]) -> None:
                    for k, v in d.items():
                        if isinstance(v, dict):
                            extract_flat(v)
                        elif isinstance(v, (str, int, float)) and v is not None:
                            flat[re.sub(r"[^a-z0-9]", "", str(k).lower())] = str(v)

                extract_flat(item)

                raw_name = (flat.get("playername") or flat.get("namefromstats") or flat.get("name") or
                            flat.get("fullname") or flat.get("displayname"))
                if not raw_name or not isinstance(raw_name, str):
                    continue

                c_name = clean_name(raw_name)
                if not is_valid_player_candidate(c_name):
                    continue

                raw_num = (flat.get("playeruniform") or flat.get("uniformnumber") or flat.get("jersey") or
                           flat.get("number") or flat.get("uniform") or "")
                raw_pos = flat.get("position") or flat.get("positionshort") or flat.get("pos") or ""
                raw_yr = flat.get("academicyear") or flat.get("year") or flat.get("class") or ""

                # Skater stats extraction
                gp = flat.get("gamesplayed") or flat.get("gp") or flat.get("totalgamesplayed") or "0"
                g = flat.get("goals") or flat.get("g") or flat.get("totalgoals") or "0"
                a = flat.get("assists") or flat.get("a") or flat.get("totalassists") or "0"
                pts = flat.get("points") or flat.get("pts") or flat.get("totalpoints") or "0"
                sh = flat.get("shots") or flat.get("sh") or flat.get("sog") or flat.get("shotsongoal") or "0"
                blk = flat.get("blocked") or flat.get("blockedshots") or flat.get("blocks") or flat.get("blk") or "0"
                ppg = flat.get("powerplaygoals") or flat.get("ppg") or flat.get("ppgoals") or "0"
                shg = flat.get("shorthandedgoals") or flat.get("shg") or flat.get("shgoals") or "0"
                gwg = flat.get("gamewinninggoals") or flat.get("gwg") or "0"

                # Penalties / PIM
                pen = flat.get("count") or flat.get("penalties") or flat.get("pen") or "0"
                pim = flat.get("minutes") or flat.get("penaltyminutes") or flat.get("pim") or "0"
                pn_pim = f"{clean_stat_int(pen)}-{clean_stat_int(pim)}"

                # Goalie stats
                has_goalie = "saves" in flat or "goalsagainstaverage" in flat or "gaa" in flat or normalize_position(str(raw_pos)) == "G"
                gp_gs = flat.get("gamesstarted") or gp
                ga = flat.get("goalsagainst") or flat.get("allgoalsallowed") or "0"
                gaa = flat.get("goalsagainstaverage") or flat.get("gaa") or "0.00"
                sv = flat.get("saves") or flat.get("sv") or "0"
                sv_pct = flat.get("savepercentage") or flat.get("svpct") or ".000"
                w_l_t = flat.get("record") or flat.get("winlosstie") or "0-0-0"
                min_played = flat.get("minutes") or flat.get("min") or "0:00"

                p = {
                    "number": clean_number(raw_num),
                    "name": c_name,
                    "position": normalize_position(str(raw_pos)),
                    "year": normalize_year(str(raw_yr)),
                    "is_goalie": has_goalie,
                    "gp": gp,
                    "g": g,
                    "a": a,
                    "pts": pts,
                    "sh": sh,
                    "blk": blk,
                    "ppg": ppg,
                    "shg": shg,
                    "gwg": gwg,
                    "pn_pim": pn_pim,
                    "gp_gs": gp_gs,
                    "ga": ga,
                    "gaa": gaa,
                    "sv": sv,
                    "sv_pct": sv_pct,
                    "w_l_t": w_l_t,
                    "min": min_played,
                }
                players.append(p)
        except Exception:
            continue
        if len(players) >= 5:
            break

    return players

# ── Table Stats Parser (HTML Fallback) ────────────────────────────────────────
def map_table_header(header_text: str) -> Optional[str]:
    raw = header_text.strip().upper()
    hk = re.sub(r"[^A-Z0-9%/+\-]", "", raw)

    if hk in ("#", "NO", "NO.", "JERSEY", "NUM", "NUMBER", "UNIFORM"):
        return "number"
    if hk in ("PLAYER", "NAME", "PLAYERNAME", "FULLNAME", "ATHLETE", "SKATER", "GOALTENDER"):
        return "name"
    if hk in ("GP", "GAMES", "GAMESPLAYED"):
        return "gp"
    if hk in ("G", "GOALS"):
        return "g"
    if hk in ("A", "AST", "ASSISTS"):
        return "a"
    if hk in ("PTS", "POINTS", "TP", "PT"):
        return "pts"
    if hk in ("SH", "SOG", "SHOTS", "SHOTSONGOAL"):
        return "sh"
    if hk in ("PPG", "PP", "PPGOALS", "POWERPLAYGOALS", "POWERPLAYG", "POWERPLAYGOAL"):
        return "ppg"
    if hk in ("SHG", "SHGOALS", "SHORTHANDEDGOALS", "SHORTHANDGOALS", "SHORTHANDEDG", "SHORTHANDG"):
        return "shg"
    if hk in ("GWG", "GW", "GAMEWINNINGGOALS", "GWGOALS", "GAMEWINNINGG"):
        return "gwg"
    if hk in ("PN-PIM", "PEN-PIM", "PEN-MIN", "PN-MIN", "PEN/PIM", "PN/PIM", "PENALTIES-PIM", "PENALTIES-MINUTES", "PENALTIES/PIM"):
        return "pn_pim"
    if hk in ("PEN", "PN", "PENALTIES"):
        return "pen"
    if hk in ("PIM", "PENALTYMINUTES", "PENMIN"):
        return "pim"
    if hk in ("BLK", "BLKS", "BS", "BLOCKS", "BLOCKEDSHOTS", "BLKD", "SHOTSBLOCKED"):
        return "blk"

    # Goalie headers
    if hk in ("GP-GS", "GP/GS", "GP_GS", "GAMES-STARTED"):
        return "gp_gs"
    if hk in ("GA", "GOALSAGAINST"):
        return "ga"
    if hk in ("GAA", "GOALSAGAINSTAVERAGE"):
        return "gaa"
    if hk in ("SV", "SAVES", "SVS"):
        return "sv"
    if hk in ("SV%", "SVPCT", "SAVE%", "SAVEPCT", "SVP"):
        return "sv_pct"
    if hk in ("W-L-T", "W-L-T-SHO", "RECORD", "WL", "W-L", "WLT"):
        return "w_l_t"
    if hk in ("MIN", "MINUTES", "TOI", "TIME"):
        return "min"

    return None

def parse_stats_tables(soup: BeautifulSoup) -> List[Dict[str, Any]]:
    players: List[Dict[str, Any]] = []

    for table in soup.find_all("table"):
        prev_heading = table.find_previous(["h2", "h3", "h4", "caption", "div"])
        prev_text = prev_heading.get_text(" ", strip=True).upper() if prev_heading else ""
        if any(bad in prev_text for bad in ["GAME RESULTS", "OVERALL TEAM STATS", "TEAM STATISTICS", "GOALS BY PERIOD", "SHOTS BY PERIOD"]):
            continue

        rows = table.find_all("tr")
        if len(rows) < 2:
            continue

        header_row = rows[0]
        cols = [c.get_text(" ", strip=True) for c in header_row.find_all(["th", "td"])]
        mapping: Dict[str, int] = {}
        for idx, col in enumerate(cols):
            f = map_table_header(col)
            if f and f not in mapping:
                mapping[f] = idx

        if len(mapping) < 3:
            continue

        is_goalie_table = ("gaa" in mapping) or ("sv_pct" in mapping) or ("sv" in mapping)
        name_idx = mapping.get("name")
        num_idx = mapping.get("number")

        for r in rows[1:]:
            cells = [c.get_text(" ", strip=True) for c in r.find_all(["td", "th"])]
            if len(cells) < 3:
                continue

            raw_name = cells[name_idx] if name_idx is not None and name_idx < len(cells) else (cells[1] if len(cells) > 1 else "")
            c_name = clean_name(raw_name)
            if not is_valid_player_candidate(c_name):
                continue

            raw_num = cells[num_idx] if num_idx is not None and num_idx < len(cells) else (cells[0] if cells[0].isdigit() else "")

            p = {
                "number": clean_number(raw_num),
                "name": c_name,
                "position": "G" if is_goalie_table else "",
                "year": "",
                "is_goalie": is_goalie_table,
            }
            for field, idx_col in mapping.items():
                if idx_col < len(cells):
                    p[field] = cells[idx_col]

            players.append(p)

    return players

# ── Section-Aware Team Stats Parser ──────────────────────────────────────────
def parse_team_stats(soup: BeautifulSoup) -> Dict[str, str]:
    found: Dict[str, str] = {}
    current_section = ""

    for table in soup.find_all("table"):
        heading = table.find_previous(["h2", "h3", "h4", "caption"])
        if heading:
            h_text = heading.get_text(" ", strip=True).upper()
            if "POWER PLAY" in h_text:
                current_section = "POWER_PLAY"
            elif "PENALT" in h_text:
                current_section = "PENALTIES"
            elif "FACEOFF" in h_text:
                current_section = "FACEOFFS"
            elif "GOAL" in h_text:
                current_section = "GOALS"
            elif "SHOT" in h_text:
                current_section = "SHOTS"

        for row in table.find_all("tr"):
            cells = [c.get_text(" ", strip=True) for c in row.find_all(["td", "th"])]
            if len(cells) < 2:
                continue

            label = cells[0].strip().lower()
            val_team = cells[1].strip() if len(cells) > 1 else ""
            val_opp = cells[2].strip() if len(cells) > 2 else ""

            if "power play" in label:
                current_section = "POWER_PLAY"
            elif "penalt" in label:
                current_section = "PENALTIES"
            elif "faceoff" in label:
                current_section = "FACEOFFS"
            elif "goals" in label:
                current_section = "GOALS"
            elif "shots" in label:
                current_section = "SHOTS"

            # 1. Power Play Conversion % & Opponent PP Kill %
            if "conversion percentage" in label or (current_section == "POWER_PLAY" and "percentage" in label):
                if val_team:
                    pp_val = _f(val_team)
                    if pp_val is not None:
                        if pp_val <= 1.0:
                            pp_val *= 100.0
                        found["pp_pct"] = f"{pp_val:.1f}%"
                if val_opp:
                    opp_float = _f(val_opp)
                    if opp_float is not None:
                        if opp_float > 1.0:
                            opp_float /= 100.0
                        pk_val = (1.0 - opp_float) * 100.0
                        found["pk_pct"] = f"{pk_val:.1f}%"

            # 2. Penalties Per Game (Pen/G)
            if ("per game" in label or "pen/g" in label or "penalties per game" in label) and current_section == "PENALTIES":
                if val_team and "pen_per_game" not in found:
                    found["pen_per_game"] = val_team

            # 3. Penalty Minutes Per Game (PIM/G)
            if ("minutes per game" in label or "pim/g" in label or "minutes/game" in label) and current_section == "PENALTIES":
                if val_team and "pim_per_game" not in found:
                    found["pim_per_game"] = val_team

            # 4. Faceoff %
            if ("percentage" in label or "faceoff %" in label or "pct" in label) and current_section == "FACEOFFS":
                if val_team and "faceoff_pct" not in found:
                    f_val = _f(val_team)
                    if f_val is not None:
                        if f_val <= 1.0:
                            f_val *= 100.0
                        found["faceoff_pct"] = f"{f_val:.1f}%"

            # 5. Goals & Shots Per Game & Shot %
            if ("per game" in label or "goals/game" in label) and current_section == "GOALS":
                if val_team and "goals_per_game" not in found:
                    found["goals_per_game"] = val_team
            if ("per game" in label or "shots/game" in label) and current_section == "SHOTS":
                if val_team and "shots_per_game" not in found:
                    found["shots_per_game"] = val_team
            if ("shot percentage" in label or "shot pct" in label or "percentage" in label) and current_section == "SHOTS":
                if val_team and "shot_pct" not in found:
                    s_val = _f(val_team)
                    if s_val is not None:
                        if s_val <= 1.0:
                            s_val *= 100.0
                        found["shot_pct"] = f"{s_val:.1f}%"

    result: Dict[str, str] = {}
    for k in TEAM_STAT_KEYS:
        result[k] = found.get(k, "-")

    return result

# ── Player Finalizer (Skater vs Goalie) ────────────────────────────────────────
def finalize_player(p: Dict[str, Any]) -> Dict[str, str]:
    pos = (p.get("position") or "").upper()
    is_goalie = pos == "G" or p.get("is_goalie") is True

    record: Dict[str, str] = {
        "number": p.get("number", ""),
        "name": p.get("name", ""),
        "position": "G" if is_goalie else (pos or "F"),
        "year": p.get("year", ""),
        "is_goalie": "true" if is_goalie else "false",
    }

    if is_goalie:
        record["gp_gs"] = str(p.get("gp_gs") or p.get("gp", "0"))
        record["ga"] = clean_stat_int(p.get("ga"))
        record["gaa"] = str(p.get("gaa") or "0.00")
        record["sv"] = clean_stat_int(p.get("sv"))
        record["sv_pct"] = clean_stat_pct(p.get("sv_pct"))
        record["w_l_t"] = str(p.get("w_l_t") or "0-0-0")
        record["min"] = str(p.get("min") or "0:00")
    else:
        pn_pim = p.get("pn_pim", "")
        if not pn_pim and (p.get("pen") or p.get("pim")):
            pn_pim = f"{clean_stat_int(p.get('pen'))}-{clean_stat_int(p.get('pim'))}"
        record["gp"] = clean_stat_int(p.get("gp"))
        record["g"] = clean_stat_int(p.get("g"))
        record["a"] = clean_stat_int(p.get("a"))
        record["pts"] = clean_stat_int(p.get("pts"))
        record["sh"] = clean_stat_int(p.get("sh"))
        record["blk"] = clean_stat_int(p.get("blk"))
        record["ppg"] = clean_stat_int(p.get("ppg"))
        record["shg"] = clean_stat_int(p.get("shg"))
        record["gwg"] = clean_stat_int(p.get("gwg"))
        record["pn_pim"] = clean_pn_pim(pn_pim)

    return record

# ── Scrape Single Team Pipeline ──────────────────────────────────────────────
async def scrape_single_team(context: BrowserContext, team_cfg: Dict[str, Any], semaphore: asyncio.Semaphore) -> Dict[str, Any]:
    async with semaphore:
        slug, label = team_cfg["slug"], team_cfg["name"]
        logger.info(f"Processing {label} (Men's Hockey)...")

        candidates = [team_cfg.get("roster_url", "")]
        stats_html, (roster_lookup, roster_used) = await asyncio.gather(
            fetch_page_html(context, team_cfg["stats_url"], wait_for_table=True),
            fetch_roster(context, candidates, label),
        )

        stats_players: List[Dict[str, Any]] = []
        style = "none"

        if stats_html:
            # 1. Try Nuxt Data first with recursive resolver
            stats_players = parse_nuxt_data_stats(stats_html)
            style = "New Style (Nuxt data)"

            active_skaters = [p for p in stats_players if not p.get("is_goalie")]
            has_scoring = any((_f(p.get("pts")) or 0) > 0 for p in active_skaters)

            # 2. Fallback to HTML table parsing if Nuxt data had 0s
            if not stats_players or not has_scoring:
                soup = BeautifulSoup(stats_html, "html.parser")
                table_players = parse_stats_tables(soup)
                if len(table_players) >= len(stats_players):
                    stats_players = table_players
                    style = "Old Style (HTML tables)"

        soup = BeautifulSoup(stats_html or "", "html.parser")
        team_stats = parse_team_stats(soup)

        # Merge roster data with player stats - STRICT ROSTER CHECK
        merged: List[Dict[str, str]] = []
        seen = set()

        for sp in stats_players:
            name = sp.get("name", "")
            if not name or name.lower() in seen or not is_valid_player_candidate(name):
                continue

            r_info = find_roster_entry(name, sp.get("number", ""), roster_lookup)

            # Strict Roster Gate: Discard unrostered match splits or game summaries
            if roster_lookup and len(roster_lookup) >= 5 and not r_info:
                continue

            seen.add(name.lower())
            p = dict(sp)
            if r_info:
                p["name"] = r_info.get("name") or name
                p["number"] = sp.get("number") or r_info.get("number", "")
                p["position"] = r_info.get("position") or sp.get("position", "")
                p["year"] = r_info.get("year") or sp.get("year", "")

            merged.append(finalize_player(p))

        team_data: Dict[str, Any] = {
            "team": label,
            "slug": slug,
            "gender": "men",
            "sport": "hockey",
            "conference": "NCHC" if "non_conf" not in slug else "Non-Conference",
            "generated_at": now_iso(),
            "source_stats_url": team_cfg["stats_url"],
            "source_roster_url": roster_used,
            "site_style": style,
            "team_stats": team_stats,
            "players": merged,
        }

        os.makedirs("docs", exist_ok=True)
        for path in [f"{slug}.json", os.path.join("docs", f"{slug}.json")]:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(team_data, f, indent=2)

        return team_data

# ── Main Runner ──────────────────────────────────────────────────────────────
async def run_scraper(only: Optional[List[str]], debug: bool) -> None:
    os.makedirs("docs", exist_ok=True)
    semaphore = asyncio.Semaphore(CONCURRENCY_LIMIT)

    teams = list(TEAMS_CONFIG) + load_non_conf_config()

    if only:
        wanted = [o.strip().lower() for o in only if o.strip()]
        teams = [t for t in teams if any(w in t["name"].lower() or w in t["slug"] for w in wanted)]

    start = time.time()
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
        context = await browser.new_context(
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
        )
        logger.info(f"=== Starting Scraping for {len(teams)} Teams ===")
        results = await asyncio.gather(
            *[scrape_single_team(context, t, semaphore) for t in teams],
            return_exceptions=True,
        )
        await browser.close()

    logger.info(f"Finished in {time.time() - start:.1f}s")

def main() -> None:
    ap = argparse.ArgumentParser(description="NCHC Men's Hockey stats scraper")
    ap.add_argument("--only", help='Comma-separated team names/slugs, e.g. "St. Thomas,Denver"')
    ap.add_argument("--debug", action="store_true", help="Debug mode")
    args = ap.parse_args()
    asyncio.run(run_scraper(args.only.split(",") if args.only else None, args.debug))

if __name__ == "__main__":
    main()
