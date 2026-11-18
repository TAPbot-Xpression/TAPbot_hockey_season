#!/usr/bin/env python3
"""
scrape_nchc_hockey.py
NCHC Men's Hockey player season stats + team stats scraper (Sidearm sites).
Supports both conference and non-conference opponents via non_conf_config.json.
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

# ── Header & Field Aliases ───────────────────────────────────────────────────
HEADER_ALIASES: Dict[str, List[str]] = {
    # Skater
    "gp": ["GP", "GAMES", "GAMESPLAYED", "GP-GS", "GP/GS"],
    "g": ["G", "GOALS"],
    "a": ["A", "AST", "ASSISTS"],
    "pts": ["PTS", "POINTS", "TP", "PT"],
    "sh": ["SH", "SOG", "SHOTS", "SHOTSONGOAL"],
    "blk": ["BLK", "BLKS", "BS", "BLOCKS", "BLOCKEDSHOTS", "BLKD", "SHOTSBLOCKED"],
    "ppg": ["PPG", "PP", "PPGOALS", "POWERPLAYGOALS", "POWERPLAYG", "POWERPLAYGOAL"],
    "shg": ["SHG", "SHGOALS", "SHORTHANDEDGOALS", "SHORTHANDGOALS", "SHORTHANDEDG"],
    "gwg": ["GWG", "GW", "GAMEWINNINGGOALS", "GWGOALS", "GAMEWINNINGG"],
    "pn_pim": ["PN-PIM", "PEN-PIM", "PEN-MIN", "PN-MIN", "PEN/PIM", "PN/PIM", "PENALTIES-PIM", "PENALTIES-MINUTES"],
    "pen": ["PEN", "PN", "PENALTIES", "PENS"],
    "pim": ["PIM", "PENALTYMINUTES", "PENMIN", "PENALTYMIN"],
    "fow": ["FOW", "FACEOFFSWON"],
    "fol": ["FOL", "FACEOFFSLOST"],
    "fo_pct": ["FO%", "FOPCT", "FACEOFF%", "FACEOFFPCT"],
    # Goalie
    "gp_gs": ["GP-GS", "GP/GS", "GP_GS", "GAMES-STARTED", "GP-GS-MIN"],
    "ga": ["GA", "GOALSAGAINST"],
    "gaa": ["GAA", "GOALSAGAINSTAVERAGE", "AVG"],
    "sv": ["SV", "SAVES", "SVS"],
    "sv_pct": ["SV%", "SVPCT", "SAVE%", "SAVEPCT", "SVP", "PCT"],
    "w_l_t": ["W-L-T", "W-L-T-SHO", "RECORD", "WL", "W-L", "WLT"],
    "min": ["MIN", "MINUTES", "TIME"],
}

NUXT_ALIASES: Dict[str, List[str]] = {
    "gp": ["gamesPlayed", "gp", "games", "gpgs", "totalGamesPlayed", "overallGames"],
    "g": ["goals", "g", "totalGoals", "scoringGoals"],
    "a": ["assists", "a", "totalAssists", "scoringAssists"],
    "pts": ["points", "pts", "totalPoints", "scoringPoints"],
    "sh": ["shots", "sh", "sog", "shotsOnGoal", "totalShots", "shotsTotal"],
    "blk": ["blockedShots", "blocks", "blk", "blks", "shotsBlocked", "blockShots"],
    "ppg": ["powerPlayGoals", "ppg", "ppGoals", "powerplayGoals"],
    "shg": ["shortHandedGoals", "shg", "shGoals", "shorthandedGoals"],
    "gwg": ["gameWinningGoals", "gwg", "gameWinners"],
    "pn_pim": ["pnPim", "penPim", "penaltiesPim", "pnpim", "penaltiesPenaltyMinutes"],
    "pen": ["penalties", "pen", "pn", "penaltyCount", "numPenalties"],
    "pim": ["penaltyMinutes", "pim", "penMin", "penaltyMins", "penMinutes"],
    "gp_gs": ["gamesStarted", "gpgs", "gpGs", "recordGpGs"],
    "ga": ["goalsAgainst", "ga", "totalGoalsAgainst"],
    "gaa": ["goalsAgainstAverage", "gaa", "avgGaa"],
    "sv": ["saves", "sv", "totalSaves"],
    "sv_pct": ["savePercentage", "savePct", "svPct", "pctSave"],
    "w_l_t": ["winLossTie", "record", "wlt", "wltRecord"],
    "min": ["minutes", "min", "timeOnIce"],
}

def _nk(text: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(text).lower())

def _hk(text: str) -> str:
    return re.sub(r"[^A-Z0-9%/+\-]", "", (text or "").upper().replace("–", "-").replace("—", "-"))

HEADER_LOOKUP: Dict[str, str] = {_hk(a): f for f, lst in HEADER_ALIASES.items() for a in lst}
NUXT_LOOKUP = {f: [_nk(a) for a in lst] for f, lst in NUXT_ALIASES.items()}
ROW_FIELDS = list(HEADER_ALIASES.keys())
NAME_HEADERS = {"PLAYER", "NAME", "PLAYERNAME", "FULLNAME", "ATHLETE", "GOALTENDER", "SKATER"}
NUM_HEADERS = {"#", "no", "no.", "jersey", "num", "number", "uniform"}

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
            def resolve(val: Any) -> str:
                if isinstance(val, int) and 0 <= val < len(data):
                    target = data[val]
                    if isinstance(target, (str, int, float)):
                        return str(target)
                return str(val) if val is not None else ""

            for item in data:
                if not isinstance(item, dict):
                    continue
                raw_name = (item.get("name") or item.get("playerName") or item.get("fullName") or item.get("displayName"))
                if not raw_name:
                    continue
                c_name = clean_name(resolve(raw_name))
                raw_num = item.get("uniform") or item.get("jersey") or item.get("number") or item.get("jerseyNumber")
                raw_pos = item.get("position") or item.get("positionShort") or item.get("pos")
                raw_yr = item.get("academicYear") or item.get("year") or item.get("class") or item.get("cl")
                _add_roster(roster_lookup, c_name, clean_number(resolve(raw_num)),
                            normalize_position(resolve(raw_pos)), normalize_year(resolve(raw_yr)))
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
            if not name_cand and len(c_clean) > 2:
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
def parse_nuxt_data_stats(html: str) -> Tuple[List[Dict[str, Any]], Dict[str, str]]:
    players: List[Dict[str, Any]] = []
    totals: Dict[str, str] = {}

    for pat in NUXT_PATTERNS:
        m = re.search(pat, html, re.DOTALL)
        if not m:
            continue
        try:
            data = json.loads(m.group(1))
            if not isinstance(data, list):
                continue

            def resolve(val: Any) -> Any:
                if isinstance(val, int) and 0 <= val < len(data):
                    return data[val]
                return val

            for item in data:
                if not isinstance(item, dict):
                    continue
                if any(k in item for k in ("boxscoreUrl", "opponent", "winOrLoss")):
                    continue

                # Flatten sub-dictionaries if present
                flat: Dict[str, Any] = {}
                for k, v in item.items():
                    res = resolve(v)
                    if isinstance(res, dict):
                        for sub_k, sub_v in res.items():
                            flat[_nk(sub_k)] = str(resolve(sub_v))
                    else:
                        flat[_nk(k)] = str(res)

                def stat(field: str) -> str:
                    for a in NUXT_LOOKUP.get(field, []):
                        if a in flat:
                            val = flat[a].strip()
                            if val and val not in ("None", "null", "undefined"):
                                return val
                    return ""

                fields = {f: stat(f) for f in ROW_FIELDS}
                has_skater_stat = any(fields.get(f) for f in ("gp", "g", "a", "pts", "sh"))
                has_goalie_stat = any(fields.get(f) for f in ("gaa", "sv", "sv_pct", "ga"))

                if not (has_skater_stat or has_goalie_stat):
                    continue

                raw_name = ""
                for k in ("playername", "namefromstats", "name", "fullname", "displayname"):
                    if k in flat and flat[k]:
                        raw_name = flat[k]
                        break
                if not raw_name:
                    continue

                c_name = clean_name(raw_name)
                if not c_name or c_name.lower() in ("total", "totals", "team"):
                    continue

                raw_num = flat.get("jersey") or flat.get("number") or flat.get("uniform") or ""
                raw_pos = flat.get("position") or flat.get("pos") or ""
                raw_yr = flat.get("year") or flat.get("class") or flat.get("academicyear") or ""

                is_goalie = normalize_position(raw_pos) == "G" or has_goalie_stat

                p = {
                    "number": clean_number(raw_num),
                    "name": c_name,
                    "position": normalize_position(raw_pos),
                    "year": normalize_year(raw_yr),
                    "is_goalie": is_goalie,
                }
                p.update(fields)
                players.append(p)
        except Exception:
            continue
        if len(players) >= 5:
            break

    return players, totals

# ── Table Stats Parser (HTML) ─────────────────────────────────────────────────
def parse_stats_tables(soup: BeautifulSoup) -> Tuple[List[Dict[str, Any]], Dict[str, str]]:
    players: List[Dict[str, Any]] = []
    totals: Dict[str, str] = {}

    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if len(rows) < 2:
            continue

        header_row = rows[0]
        cols = [_hk(c.get_text(" ", strip=True)) for c in header_row.find_all(["th", "td"])]
        mapping = {}
        for idx, col in enumerate(cols):
            f = HEADER_LOOKUP.get(col)
            if f:
                mapping[f] = idx

        is_goalie_table = ("gaa" in mapping) or ("sv_pct" in mapping) or ("saves" in mapping)

        name_idx = None
        for idx, col in enumerate(cols):
            if col in NAME_HEADERS:
                name_idx = idx
                break

        for r in rows[1:]:
            cells = [c.get_text(" ", strip=True) for c in r.find_all(["td", "th"])]
            if len(cells) < 3:
                continue

            raw_name = cells[name_idx] if name_idx is not None and name_idx < len(cells) else cells[1]
            c_name = clean_name(raw_name)
            if not c_name or c_name.lower() in ("total", "totals", "team", "opponent", "opponents"):
                continue

            fields = {f: cells[mapping[f]] for f in mapping if mapping[f] < len(cells)}
            raw_num = cells[0] if cells[0].isdigit() else ""

            p = {
                "number": clean_number(raw_num),
                "name": c_name,
                "position": "G" if is_goalie_table else "",
                "year": "",
                "is_goalie": is_goalie_table,
            }
            p.update(fields)
            players.append(p)

    return players, totals

# ── Section-Aware Team Stats Parser ──────────────────────────────────────────
def parse_team_stats(soup: BeautifulSoup, totals: Dict[str, str]) -> Dict[str, str]:
    """
    Parses Sidearm's team stats tables using section headers:
    POWER PLAY -> Conversion Percentage (Col 1: Home, Col 2: Opponent)
    PENALTIES  -> Per Game, Minutes Per Game
    FACEOFFS   -> Percentage
    """
    found: Dict[str, str] = {}
    current_section = ""

    for table in soup.find_all("table"):
        # Check previous heading or caption for section context
        heading = table.find_previous(["h2", "h3", "h4", "caption", "div"])
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

            # Check if row is a section header inside table
            if "power play" in label:
                current_section = "POWER_PLAY"
            elif "penalt" in label:
                current_section = "PENALTIES"
            elif "faceoff" in label:
                current_section = "FACEOFFS"

            # 1. Power Play Conversion % & Opponent PP Kill %
            if "conversion percentage" in label or "pp pct" in label or ("percentage" in label and current_section == "POWER_PLAY"):
                if val_team:
                    found["pp_pct"] = clean_stat_pct(val_team)
                if val_opp:
                    # Invert opponent power play % into penalty kill %
                    opp_float = _f(val_opp)
                    if opp_float is not None:
                        if opp_float > 1.0:
                            opp_float /= 100.0
                        pk_val = (1.0 - opp_float) * 100.0
                        found["pk_pct"] = f"{pk_val:.1f}%"

            # 2. Penalties Per Game
            if ("per game" in label or "pen/g" in label) and current_section == "PENALTIES":
                if val_team and "pen_per_game" not in found:
                    found["pen_per_game"] = val_team

            # 3. Penalty Minutes Per Game (PIM/G)
            if ("minutes per game" in label or "pim/g" in label or "pim per game" in label):
                if val_team and "pim_per_game" not in found:
                    found["pim_per_game"] = val_team

            # 4. Faceoff %
            if ("percentage" in label or "pct" in label or "faceoff" in label) and current_section == "FACEOFFS":
                if val_team and "faceoff_pct" not in found:
                    f_val = _f(val_team)
                    if f_val is not None:
                        if f_val <= 1.0:
                            f_val *= 100.0
                        found["faceoff_pct"] = f"{f_val:.1f}%"

            # 5. Goals & Shots Per Game
            if "goals/game" in label or "goals per game" in label or ("per game" in label and current_section == "GOALS"):
                found.setdefault("goals_per_game", val_team)
            if "shots/game" in label or "shots per game" in label or ("per game" in label and current_section == "SHOTS"):
                found.setdefault("shots_per_game", val_team)
            if "shot percentage" in label or "shot pct" in label or "shot %" in label:
                found.setdefault("shot_pct", clean_stat_pct(val_team))

    # Format team stats
    result: Dict[str, str] = {}
    for k in TEAM_STAT_KEYS:
        val = found.get(k, "-")
        if val != "-" and not val.endswith("%") and ("pct" in k):
            pct_f = _f(val)
            val = f"{pct_f * 100:.1f}%" if pct_f and pct_f <= 1.0 else f"{val}%"
        result[k] = val

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
        record["gp_gs"] = p.get("gp_gs") or p.get("gp", "0")
        record["ga"] = clean_stat_int(p.get("ga"))
        record["gaa"] = p.get("gaa") or "0.00"
        record["sv"] = clean_stat_int(p.get("sv"))
        record["sv_pct"] = clean_stat_pct(p.get("sv_pct"))
        record["w_l_t"] = p.get("w_l_t") or "0-0-0"
        record["min"] = p.get("min") or "0:00"
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
        totals: Dict[str, str] = {}
        style = "none"

        if stats_html:
            # 1. Try Nuxt Data first
            stats_players, totals = parse_nuxt_data_stats(stats_html)
            style = "New Style (Nuxt data)"

            # Check if Nuxt stats came back empty or all zeros for goals/points
            active_skaters = [p for p in stats_players if not p.get("is_goalie")]
            has_scoring = any((_f(p.get("pts")) or 0) > 0 for p in active_skaters)

            # 2. Fallback to HTML table parsing if Nuxt data had 0s
            if not stats_players or not has_scoring:
                soup = BeautifulSoup(stats_html, "html.parser")
                table_players, t_totals = parse_stats_tables(soup)
                if len(table_players) >= len(stats_players):
                    stats_players = table_players
                    totals = t_totals
                    style = "Old Style (HTML tables)"

        soup = BeautifulSoup(stats_html or "", "html.parser")
        team_stats = parse_team_stats(soup, totals)

        # Merge roster data with player stats
        merged: List[Dict[str, str]] = []
        seen = set()
        for sp in stats_players:
            name = sp.get("name", "")
            if not name or name.lower() in seen:
                continue
            seen.add(name.lower())

            r_info = find_roster_entry(name, sp.get("number", ""), roster_lookup)
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

    # Combine conference teams + any non-conf teams in non_conf_config.json
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
