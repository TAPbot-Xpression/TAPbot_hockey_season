#!/usr/bin/env python3
"""
scrape_nchc_hockey.py
NCHC Men's Hockey player season stats + team stats scraper.

Key Updates:
1. Team Stats Tab Selection (New Style Sidearm):
   - Clicks the 'Team Stats' tab (<button data-test-id="s-tab__link"> Team Stats </button>)
     to load the official team summary table.
2. Section-Scoped Team Stats Extraction:
   - [Shots Group]:
       * Goals Per Game -> goals_per_game (e.g. 2.5)
       * Shot Percentage -> shot_pct (e.g. 8.3%)
       * Shots Per Game -> shots_per_game (e.g. 30.2)
   - [Power Plays Group]:
       * Conversion Percentage (Home) -> pp_pct (e.g. 22.2%)
       * Conversion Percentage (Opponent) -> inverted to Home PK% (1.000 - 0.235 = 76.5%) -> pk_pct
   - [Penalties Group]:
       * Per Game -> pen_per_game (e.g. 4.6)
       * Minutes Per Game -> pim_per_game (e.g. 12.5)
   - [Faceoffs Group]:
       * Percentage -> faceoff_pct (e.g. 52.0%)
3. Goaltender Record:
   - Parses individual 'W', 'L', and 'T' columns into 'W-L-T' format (e.g., 3-3-0).
4. Output Directory:
   - Exclusively saves JSON files to 'docs/<slug>.json'.
   - Non-conference opponent is strictly saved as 'docs/non_conf_mhockey.json'.
5. Old-Style Sites (style == "old", e.g. Colorado College) use their own pipeline
   (scrape_single_team_old_style); the new-style pipeline is untouched:
   - Confirms jQuery UI tab selection (<li role="tab" aria-controls="team" aria-selected="true">).
   - Player tables have two-row grouped headers; parsed via colspan/rowspan flattening.
   - Team stats section titles are single-cell rows; parsed by section.
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
MIN_ROSTER_ENTRIES = 10

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

def clean_w_l_t(val: Any) -> str:
    if val is None:
        return "0-0-0"
    s = str(val).strip().replace("–", "-").replace("—", "-")
    m = re.match(r"^(\d+)\s*-\s*(\d+)(?:\s*-\s*(\d+))?$", s)
    if m:
        w = int(m.group(1))
        l = int(m.group(2))
        t = int(m.group(3)) if m.group(3) is not None else 0
        return f"{w}-{l}-{t}"
    return s if s and s != "-" else "0-0-0"

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
                    teams.append({
                        "name": name,
                        "slug": "non_conf_mhockey",
                        "style": "new",
                        "stats_url": info["stats_url"],
                        "roster_url": info.get("roster_url") or "",
                    })
        return teams
    except Exception as e:
        logger.warning(f"Could not load non_conf_config.json: {e}")
        return []

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

async def fetch_roster(context: BrowserContext, roster_url: str, label: str) -> Dict[str, Dict[str, str]]:
    roster_lookup: Dict[str, Dict[str, str]] = {}
    if not roster_url:
        return roster_lookup

    page: Optional[Page] = None
    try:
        page = await context.new_page()
        await page.goto(roster_url, wait_until="domcontentloaded", timeout=25000)
        try:
            await page.wait_for_load_state("networkidle", timeout=3000)
        except Exception:
            pass
        html = await page.content()
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

        for card in soup.select('[class*="roster-player"], [class*="s-person-card"]'):
            name_el = card.select_one('[class*="player-name"], [class*="name"] a, a[href*="/roster/"]')
            if not name_el:
                continue
            c_name = clean_name(name_el.get_text(strip=True))
            if not is_valid_player_candidate(c_name):
                continue
            jersey_el = card.select_one('[class*="jersey"], [class*="number"]')
            num = clean_number(jersey_el.get_text(strip=True)) if jersey_el else ""
            pos_el = card.select_one('[class*="position"]')
            pos = normalize_position(pos_el.get_text(strip=True)) if pos_el else ""
            yr_el = card.select_one('[class*="year"], [class*="class"]')
            yr = normalize_year(yr_el.get_text(strip=True)) if yr_el else ""
            _add_roster(roster_lookup, c_name, num, pos, yr)

    except Exception as e:
        logger.warning(f"[{label}] Error fetching roster: {e}")
    finally:
        if page:
            await page.close()

    return roster_lookup

# ── Header Normalization & Mappers ───────────────────────────────────────────
def map_skater_header(header_text: str) -> Optional[str]:
    hk = re.sub(r"[^A-Z0-9%/+\-]", "", header_text.strip().upper())
    if hk in ("#", "NO", "NO.", "JERSEY", "NUM", "NUMBER", "UNIFORM"):
        return "number"
    if hk in ("PLAYER", "NAME", "PLAYERNAME", "FULLNAME", "ATHLETE", "SKATER"):
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
    if hk in ("BLK", "BLKS", "BS", "BLOCKS", "BLOCKEDSHOTS", "BLKD", "SHOTSBLOCKED"):
        return "blk"
    if hk in ("PN-PIM", "PEN-PIM", "PEN-MIN", "PN-MIN", "PEN/PIM", "PN/PIM", "PENALTIES-PIM", "PENALTIES-MINUTES", "PENALTIES/PIM", "PN/MIN"):
        return "pn_pim"
    if hk in ("PEN", "PN", "PENALTIES"):
        return "pen"
    if hk in ("PIM", "PENALTYMINUTES", "PENMIN"):
        return "pim"
    return None

def map_goalie_header(header_text: str) -> Optional[str]:
    hk = re.sub(r"[^A-Z0-9%/+\-]", "", header_text.strip().upper())
    if hk in ("#", "NO", "NO.", "JERSEY", "NUM", "NUMBER", "UNIFORM"):
        return "number"
    if hk in ("PLAYER", "NAME", "PLAYERNAME", "FULLNAME", "ATHLETE", "GOALTENDER", "GOALIE"):
        return "name"
    if hk in ("GP-GS", "GP/GS", "GP_GS", "GAMES-STARTED", "GP/GS/MIN"):
        return "gp_gs"
    if hk in ("GP", "GAMES"):
        return "gp"
    if hk in ("GS", "STARTS"):
        return "gs"
    if hk in ("GA", "GOALSAGAINST", "GOALSALLOWED"):
        return "ga"
    if hk in ("GAA", "GOALSAGAINSTAVERAGE", "AVG"):
        return "gaa"
    if hk in ("SV", "SAVES", "SVS"):
        return "sv"
    if hk in ("SV%", "SVPCT", "SAVE%", "SAVEPCT", "SVP"):
        return "sv_pct"
    if hk in ("SHO", "SO", "SHUTOUTS"):
        return "sho"
    if hk in ("W", "WIN", "WINS"):
        return "w"
    if hk in ("L", "LOSS", "LOSSES"):
        return "l"
    if hk in ("T", "TIE", "TIES"):
        return "t"
    if hk in ("W-L-T", "W-L-T-SHO", "RECORD", "WL", "W-L", "WLT"):
        return "w_l_t"
    if hk in ("MIN", "MINUTES", "TOI", "TIME"):
        return "min"
    return None

# ── Table Parser Helpers ──────────────────────────────────────────────────────
def parse_skaters_table(soup: BeautifulSoup) -> List[Dict[str, Any]]:
    skaters: List[Dict[str, Any]] = []
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if len(rows) < 2:
            continue

        header_row = rows[0]
        cols = [c.get_text(" ", strip=True) for c in header_row.find_all(["th", "td"])]
        mapping: Dict[str, int] = {}
        for idx, col in enumerate(cols):
            f = map_skater_header(col)
            if f and f not in mapping:
                mapping[f] = idx

        if not ("g" in mapping and "pts" in mapping):
            continue

        name_idx = mapping.get("name")
        num_idx = mapping.get("number")

        for r in rows[1:]:
            cells = [c.get_text(" ", strip=True) for c in r.find_all(["td", "th"])]
            if len(cells) < 4:
                continue

            raw_name = cells[name_idx] if name_idx is not None and name_idx < len(cells) else cells[1]
            c_name = clean_name(raw_name)
            if not is_valid_player_candidate(c_name):
                continue

            raw_num = cells[num_idx] if num_idx is not None and num_idx < len(cells) else (cells[0] if cells[0].isdigit() else "")

            p = {
                "number": clean_number(raw_num),
                "name": c_name,
                "position": "F",
                "year": "",
                "is_goalie": False,
                "gp": cells[mapping["gp"]] if "gp" in mapping and mapping["gp"] < len(cells) else "0",
                "g": cells[mapping["g"]] if "g" in mapping and mapping["g"] < len(cells) else "0",
                "a": cells[mapping["a"]] if "a" in mapping and mapping["a"] < len(cells) else "0",
                "pts": cells[mapping["pts"]] if "pts" in mapping and mapping["pts"] < len(cells) else "0",
                "sh": cells[mapping["sh"]] if "sh" in mapping and mapping["sh"] < len(cells) else "0",
                "blk": cells[mapping["blk"]] if "blk" in mapping and mapping["blk"] < len(cells) else "0",
                "ppg": cells[mapping["ppg"]] if "ppg" in mapping and mapping["ppg"] < len(cells) else "0",
                "shg": cells[mapping["shg"]] if "shg" in mapping and mapping["shg"] < len(cells) else "0",
                "gwg": cells[mapping["gwg"]] if "gwg" in mapping and mapping["gwg"] < len(cells) else "0",
            }

            if "pn_pim" in mapping and mapping["pn_pim"] < len(cells):
                p["pn_pim"] = cells[mapping["pn_pim"]]
            elif "pen" in mapping and "pim" in mapping and mapping["pen"] < len(cells) and mapping["pim"] < len(cells):
                p["pn_pim"] = f"{cells[mapping['pen']]}-{cells[mapping['pim']]}"
            else:
                p["pn_pim"] = "0-0"

            skaters.append(p)

    return skaters

def parse_goalies_table(soup: BeautifulSoup) -> List[Dict[str, Any]]:
    goalies: List[Dict[str, Any]] = []
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if len(rows) < 2:
            continue

        header_row = rows[0]
        cols = [c.get_text(" ", strip=True) for c in header_row.find_all(["th", "td"])]
        mapping: Dict[str, int] = {}
        for idx, col in enumerate(cols):
            f = map_goalie_header(col)
            if f and f not in mapping:
                mapping[f] = idx

        if not ("gaa" in mapping or "sv_pct" in mapping or "sv" in mapping):
            continue

        name_idx = mapping.get("name")
        num_idx = mapping.get("number")

        for r in rows[1:]:
            cells = [c.get_text(" ", strip=True) for c in r.find_all(["td", "th"])]
            if len(cells) < 4:
                continue

            raw_name = cells[name_idx] if name_idx is not None and name_idx < len(cells) else cells[1]
            c_name = clean_name(raw_name)
            if not is_valid_player_candidate(c_name):
                continue

            raw_num = cells[num_idx] if num_idx is not None and num_idx < len(cells) else (cells[0] if cells[0].isdigit() else "")

            gp_val = cells[mapping["gp"]] if "gp" in mapping and mapping["gp"] < len(cells) else "0"
            gs_val = cells[mapping["gs"]] if "gs" in mapping and mapping["gs"] < len(cells) else "0"
            gp_gs_str = cells[mapping["gp_gs"]] if "gp_gs" in mapping and mapping["gp_gs"] < len(cells) else f"{gp_val}-{gs_val}"

            if "w" in mapping and "l" in mapping:
                w_val = clean_stat_int(cells[mapping["w"]]) if mapping["w"] < len(cells) else "0"
                l_val = clean_stat_int(cells[mapping["l"]]) if mapping["l"] < len(cells) else "0"
                t_val = clean_stat_int(cells[mapping["t"]]) if "t" in mapping and mapping["t"] < len(cells) else "0"
                wlt_str = f"{w_val}-{l_val}-{t_val}"
            elif "w_l_t" in mapping and mapping["w_l_t"] < len(cells):
                wlt_str = clean_w_l_t(cells[mapping["w_l_t"]])
            else:
                wlt_str = "0-0-0"

            g = {
                "number": clean_number(raw_num),
                "name": c_name,
                "position": "G",
                "year": "",
                "is_goalie": True,
                "gp_gs": gp_gs_str,
                "ga": cells[mapping["ga"]] if "ga" in mapping and mapping["ga"] < len(cells) else "0",
                "gaa": cells[mapping["gaa"]] if "gaa" in mapping and mapping["gaa"] < len(cells) else "0.00",
                "sv": cells[mapping["sv"]] if "sv" in mapping and mapping["sv"] < len(cells) else "0",
                "sv_pct": cells[mapping["sv_pct"]] if "sv_pct" in mapping and mapping["sv_pct"] < len(cells) else ".000",
                "sho": cells[mapping["sho"]] if "sho" in mapping and mapping["sho"] < len(cells) else "0",
                "w_l_t": wlt_str,
                "min": cells[mapping["min"]] if "min" in mapping and mapping["min"] < len(cells) else "0:00",
            }
            goalies.append(g)

    return goalies

# ── Section-Scoped Team Stats Parser ─────────────────────────────────────────
def parse_team_stats(soup: BeautifulSoup) -> Dict[str, str]:
    found: Dict[str, str] = {}
    current_section = ""

    for table in soup.find_all("table"):
        # Check table headers and sub-headers for section scope
        for row in table.find_all("tr"):
            th_cells = row.find_all("th")
            if th_cells:
                th_text = " ".join(c.get_text(" ", strip=True) for c in th_cells).upper()
                if "SHOT" in th_text:
                    current_section = "SHOTS"
                    continue
                elif "POWER PLAY" in th_text:
                    current_section = "POWER_PLAYS"
                    continue
                elif "PENALT" in th_text:
                    current_section = "PENALTIES"
                    continue
                elif "FACEOFF" in th_text:
                    current_section = "FACEOFFS"
                    continue
                elif "GOAL BREAKDOWN" in th_text or "GOAL" in th_text:
                    current_section = "GOALS"
                    continue

            cells = [c.get_text(" ", strip=True) for c in row.find_all(["td", "th"])]
            if len(cells) < 2:
                continue

            label = cells[0].strip().lower()
            val_home = cells[1].strip() if len(cells) > 1 else ""
            val_opp = cells[2].strip() if len(cells) > 2 else ""

            # 1. Under Shots Group: Goals Per Game, Shot Percentage, Shots Per Game
            if current_section == "SHOTS":
                if ("goals per game" in label or "goals/game" in label) and "goals_per_game" not in found:
                    found["goals_per_game"] = val_home
                elif ("shot percentage" in label or "shot pct" in label or "shot %" in label) and "shot_pct" not in found:
                    s_val = _f(val_home)
                    if s_val is not None:
                        if s_val <= 1.0:
                            s_val *= 100.0
                        found["shot_pct"] = f"{s_val:.1f}%"
                elif ("shots per game" in label or "shots/game" in label) and "shots_per_game" not in found:
                    found["shots_per_game"] = val_home

            # 2. Under Power Plays Group: Conversion Percentage (Home PP% and Opponent inverted PK%)
            elif current_section == "POWER_PLAYS":
                if "conversion percentage" in label or "percentage" in label or "pp pct" in label:
                    if val_home and "pp_pct" not in found:
                        pp_val = _f(val_home)
                        if pp_val is not None:
                            if pp_val <= 1.0:
                                pp_val *= 100.0
                            found["pp_pct"] = f"{pp_val:.1f}%"
                    if val_opp and "pk_pct" not in found:
                        opp_float = _f(val_opp)
                        if opp_float is not None:
                            if opp_float > 1.0:
                                opp_float /= 100.0
                            pk_val = (1.0 - opp_float) * 100.0
                            found["pk_pct"] = f"{pk_val:.1f}%"

            # 3. Under Penalties Group: Per Game and Minutes Per Game
            elif current_section == "PENALTIES":
                if (label == "per game" or "pen/g" in label or "penalties per game" in label) and "pen_per_game" not in found:
                    found["pen_per_game"] = val_home
                elif ("minutes per game" in label or "pim/g" in label or "minutes/game" in label) and "pim_per_game" not in found:
                    found["pim_per_game"] = val_home

            # 4. Under Faceoffs Group: Percentage
            elif current_section == "FACEOFFS":
                if ("percentage" in label or "pct" in label or "faceoff" in label) and "faceoff_pct" not in found:
                    f_val = _f(val_home)
                    if f_val is not None:
                        if f_val <= 1.0:
                            f_val *= 100.0
                        found["faceoff_pct"] = f"{f_val:.1f}%"

    result: Dict[str, str] = {}
    for k in TEAM_STAT_KEYS:
        result[k] = found.get(k, "-")

    return result

# ══ OLD-STYLE (jQuery UI tabs) SIDEARM SUPPORT ═══════════════════════════════
# Everything below this banner (up to "Finalize Player Dict") is used ONLY for
# teams with style == "old". New-style code paths above/below are untouched.
#
# Old-style pages differ from new-style in three ways:
#   * Player tables have a TWO-ROW grouped header (colspan/rowspan), e.g.
#       Skaters : # | Player | GP | SHOTS(G A PTS SH SH% +/-) | GOALS(PPG..UAG) | PENALTIES(PN-PIM MIN MAJ OTH) | BLK
#       Goalies : # | Player | GP-GS | MIN | Goals(GA AVG PPG SHG ENG) | Saves(SV SV%) | Record(W L T SHO)
#   * Team stats section titles ("Shots", "Power Plays", ...) are single-cell
#     <td> rows, not <th>.
#   * Tabs are jQuery UI: <li role="tab" aria-controls="team"><a href="#team">.
def _table_caption(table) -> str:
    cap = table.find("caption")
    return cap.get_text(" ", strip=True).lower() if cap else ""

def _candidate_tables(soup: BeautifulSoup) -> List[Any]:
    """All tables, minus 'Conference' duplicates when an Overall table exists."""
    tables = soup.find_all("table")
    non_conf = [t for t in tables if "conference" not in _table_caption(t)]
    return non_conf or tables

def _header_row_list(table) -> List[Any]:
    """Leading header rows: <thead> rows if present, else leading all-<th> rows."""
    thead = table.find("thead")
    if thead:
        rows = thead.find_all("tr")
        if rows:
            return rows
    header_rows: List[Any] = []
    for r in table.find_all("tr"):
        cells = r.find_all(["th", "td"])
        if cells and all(c.name == "th" for c in cells):
            header_rows.append(r)
        else:
            break
    if not header_rows:
        first = table.find("tr")
        if first:
            header_rows = [first]
    return header_rows

def build_header_labels(table) -> Tuple[List[str], List[Any]]:
    """
    Flatten a (possibly multi-row, colspan/rowspan) header into one label per
    column. The bottom-most non-empty label wins, so group titles such as
    "Shots"/"Goals"/"Penalties" are replaced by their child labels (G, A, ...).
    Returns (labels, body_rows).
    """
    header_rows = _header_row_list(table)
    grid: Dict[Tuple[int, int], str] = {}
    max_col = 0
    for ri, r in enumerate(header_rows):
        col = 0
        for c in r.find_all(["th", "td"]):
            while (ri, col) in grid:
                col += 1
            try:
                cs = max(1, int(c.get("colspan", 1) or 1))
                rs = max(1, int(c.get("rowspan", 1) or 1))
            except ValueError:
                cs, rs = 1, 1
            text = c.get_text(" ", strip=True)
            for dr in range(rs):
                for dc in range(cs):
                    grid[(ri + dr, col + dc)] = text
            col += cs
            max_col = max(max_col, col)

    n_rows = len(header_rows)
    labels: List[str] = []
    for ci in range(max_col):
        label = ""
        for ri in range(n_rows - 1, -1, -1):
            t = grid.get((ri, ci), "")
            if t:
                label = t
                break
        labels.append(label)

    all_rows = table.find_all("tr")
    body_rows = [r for r in all_rows if r not in header_rows]
    return labels, body_rows

def _grouped_mapping(table, mapper) -> Tuple[Dict[str, int], List[Any], int]:
    labels, body_rows = build_header_labels(table)
    mapping: Dict[str, int] = {}
    for idx, lab in enumerate(labels):
        f = mapper(lab) if lab else None
        if f and f not in mapping:
            mapping[f] = idx

    # Safety net: if header width != body width, right-align the header to the body.
    widths = [len(r.find_all(["td", "th"])) for r in body_rows if r.find_all(["td", "th"])]
    body_w = max(set(widths), key=widths.count) if widths else len(labels)
    if body_w != len(labels):
        shift = body_w - len(labels)
        mapping = {k: v + shift for k, v in mapping.items() if v + shift >= 0}
    return mapping, body_rows, body_w

def _cell(cells: List[str], mapping: Dict[str, int], key: str, default: str) -> str:
    i = mapping.get(key)
    return cells[i] if i is not None and i < len(cells) and cells[i] != "" else default

def parse_skaters_table_grouped(soup: BeautifulSoup) -> List[Dict[str, Any]]:
    skaters: List[Dict[str, Any]] = []
    for table in _candidate_tables(soup):
        mapping, body_rows, _ = _grouped_mapping(table, map_skater_header)
        if not ("g" in mapping and "pts" in mapping and "gp" in mapping):
            continue
        # goalie tables never have G+PTS+GP together, but skip anything with GP-GS just in case
        name_idx = mapping.get("name", 1)
        num_idx = mapping.get("number", 0)

        for r in body_rows:
            cells = [c.get_text(" ", strip=True) for c in r.find_all(["td", "th"])]
            if len(cells) < 6:
                continue
            c_name = clean_name(cells[name_idx] if name_idx < len(cells) else "")
            if not is_valid_player_candidate(c_name):
                continue
            raw_num = cells[num_idx] if num_idx < len(cells) else ""

            p: Dict[str, Any] = {
                "number": clean_number(raw_num),
                "name": c_name,
                "position": "F",
                "year": "",
                "is_goalie": False,
            }
            for key in ("gp", "g", "a", "pts", "sh", "blk", "ppg", "shg", "gwg"):
                p[key] = _cell(cells, mapping, key, "0")

            if "pn_pim" in mapping:
                p["pn_pim"] = _cell(cells, mapping, "pn_pim", "0-0")
            elif "pen" in mapping and "pim" in mapping:
                p["pn_pim"] = f"{_cell(cells, mapping, 'pen', '0')}-{_cell(cells, mapping, 'pim', '0')}"
            else:
                p["pn_pim"] = "0-0"
            skaters.append(p)
    return skaters

def parse_goalies_table_grouped(soup: BeautifulSoup) -> List[Dict[str, Any]]:
    goalies: List[Dict[str, Any]] = []
    for table in _candidate_tables(soup):
        mapping, body_rows, _ = _grouped_mapping(table, map_goalie_header)
        if not (("gp_gs" in mapping or "gp" in mapping) and ("gaa" in mapping or "sv_pct" in mapping)):
            continue
        name_idx = mapping.get("name", 1)
        num_idx = mapping.get("number", 0)

        for r in body_rows:
            cells = [c.get_text(" ", strip=True) for c in r.find_all(["td", "th"])]
            if len(cells) < 6:
                continue
            c_name = clean_name(cells[name_idx] if name_idx < len(cells) else "")
            if not is_valid_player_candidate(c_name):
                continue
            raw_num = cells[num_idx] if num_idx < len(cells) else ""

            if "gp_gs" in mapping:
                gp_gs = _cell(cells, mapping, "gp_gs", "0-0")
            else:
                gp_gs = f"{_cell(cells, mapping, 'gp', '0')}-{_cell(cells, mapping, 'gs', '0')}"

            if "w" in mapping and "l" in mapping:
                wlt = (f"{clean_stat_int(_cell(cells, mapping, 'w', '0'))}-"
                       f"{clean_stat_int(_cell(cells, mapping, 'l', '0'))}-"
                       f"{clean_stat_int(_cell(cells, mapping, 't', '0'))}")
            else:
                wlt = clean_w_l_t(_cell(cells, mapping, "w_l_t", "0-0-0"))

            gaa_f = _f(_cell(cells, mapping, "gaa", "0"))
            goalies.append({
                "number": clean_number(raw_num),
                "name": c_name,
                "position": "G",
                "year": "",
                "is_goalie": True,
                "gp_gs": gp_gs,
                "ga": _cell(cells, mapping, "ga", "0"),
                "gaa": f"{gaa_f:.2f}" if gaa_f is not None else "0.00",
                "sv": _cell(cells, mapping, "sv", "0"),
                "sv_pct": _cell(cells, mapping, "sv_pct", ".000"),
                "sho": _cell(cells, mapping, "sho", "0"),
                "w_l_t": wlt,
                "min": _cell(cells, mapping, "min", "0:00"),
            })
    return goalies

_OLD_SECTIONS = {
    "shots": "SHOTS",
    "power plays": "POWER_PLAYS", "power play": "POWER_PLAYS",
    "penalties": "PENALTIES",
    "faceoffs": "FACEOFFS", "faceoff": "FACEOFFS",
    "goal breakdown": "GOALS",
}

def _pct_str(raw: Any) -> Optional[str]:
    v = _f(raw)
    if v is None:
        return None
    if v <= 1.0:
        v *= 100.0
    return f"{v:.1f}%"

def _norm(text: Any) -> str:
    """Lower-case and collapse ALL whitespace (including &nbsp;) to single spaces."""
    return " ".join(str(text).split()).lower()

def _team_table_rows(soup: BeautifulSoup) -> List[List[str]]:
    """Rows of the first team table (normalized cell text), preferring 'Overall Team'."""
    tables = soup.find_all("table")
    ordered = [t for t in tables if "team" in _table_caption(t) and "conference" not in _table_caption(t)]
    ordered += [t for t in tables if not any(t is o for o in ordered)]
    for table in ordered:
        rows = [[" ".join(c.get_text(" ", strip=True).split()) for c in r.find_all(["td", "th"])]
                for r in table.find_all("tr")]
        flat = " | ".join(r[0].lower() for r in rows if r)
        if "goals per game" in flat or "shots per game" in flat or "conversion percentage" in flat:
            return rows
    return []

def parse_team_stats_old(soup: BeautifulSoup) -> Dict[str, str]:
    """
    Old-style Team tab: rows are [label, team value, opponent value] grouped under
    section title rows (Shots / Power Plays / Goal Breakdown / Penalties / Faceoffs).
    Values like .111 / .231 are decimals. Section titles are detected by name, and
    if the page's markup hides them, by anchor rows (Goals-Shots, Goals-Opportunities,
    Hat Tricks, Won-Lost).
    """
    found: Dict[str, str] = {}
    section = ""
    pending_pen = False

    for cells in _team_table_rows(soup):
        if not cells:
            continue
        n0 = _norm(cells[0])
        rest_empty = not any(c.strip() for c in cells[1:])

        if n0 in _OLD_SECTIONS and rest_empty:
            section = _OLD_SECTIONS[n0]
            pending_pen = False
            continue
        if len(cells) < 2:
            continue

        # Anchor rows (fallback section detection)
        if n0 == "goals-shots":
            section = "SHOTS"
        elif n0 == "goals-opportunities":
            section = "POWER_PLAYS"
        elif n0 == "won-lost":
            section = "FACEOFFS"
        elif n0 in ("hat tricks", "empty net", "short handed", "unassisted"):
            section = "GOALS"
            pending_pen = (n0 == "hat tricks")
        elif pending_pen:
            section = "PENALTIES"
            pending_pen = False

        team_val = cells[1].strip()
        opp_val = cells[2].strip() if len(cells) > 2 else ""

        if section == "SHOTS":
            if "goals per game" in n0 and "goals_per_game" not in found and team_val:
                found["goals_per_game"] = team_val
            elif ("shot percentage" in n0 or "shot pct" in n0) and "shot_pct" not in found:
                v = _pct_str(team_val)
                if v:
                    found["shot_pct"] = v
            elif "shots per game" in n0 and "shots_per_game" not in found and team_val:
                found["shots_per_game"] = team_val

        elif section == "POWER_PLAYS":
            if "conversion" in n0 or "percentage" in n0:
                if "pp_pct" not in found:
                    v = _pct_str(team_val)
                    if v:
                        found["pp_pct"] = v
                if "pk_pct" not in found:
                    opp = _f(opp_val)
                    if opp is not None:
                        if opp > 1.0:
                            opp /= 100.0
                        found["pk_pct"] = f"{(1.0 - opp) * 100.0:.1f}%"

        elif section == "PENALTIES":
            # Label cell can hold two spans, e.g. "Per Game PN/AVG", so match loosely.
            if "per game" in n0 and "minute" not in n0 and "pen_per_game" not in found and team_val:
                found["pen_per_game"] = team_val
            elif "minutes per game" in n0 and "pim_per_game" not in found and team_val:
                found["pim_per_game"] = team_val

        elif section == "FACEOFFS":
            if ("percentage" in n0 or "pct" in n0) and "faceoff_pct" not in found:
                v = _pct_str(team_val)
                if v:
                    found["faceoff_pct"] = v

    return {k: found.get(k, "-") for k in TEAM_STAT_KEYS}

def team_debug_dump(soup: BeautifulSoup, max_rows: int = 60) -> str:
    """Compact text description of the page's tables, for the Actions log."""
    lines = [f"tables on page: {len(soup.find_all('table'))}"]
    for i, t in enumerate(soup.find_all("table")[:12]):
        lines.append(f"  table[{i}] caption={_table_caption(t)!r} rows={len(t.find_all('tr'))}")
    rows = _team_table_rows(soup)
    if not rows:
        lines.append("no table containing team-stat labels was found")
    for r in rows[:max_rows]:
        lines.append("  " + repr(r))
    return "\n".join(lines)

async def _ensure_ui_tab(page: Page, panel_id: str) -> bool:
    """
    Make a jQuery-UI tab active and confirm it, e.g. for the Team tab:
      <li role="tab" aria-controls="team" aria-selected="true"><a href="#team">Team</a></li>
    """
    tab = page.locator(f'li[role="tab"][aria-controls="{panel_id}"]').first
    try:
        if await tab.count() == 0:
            return False
        if (await tab.get_attribute("aria-selected")) != "true":
            link = tab.locator("a").first
            try:
                await link.click(timeout=3000)
            except Exception:
                await link.evaluate("el => el.click()")  # e.g. cookie banner in the way
            await page.wait_for_timeout(600)
        return (await tab.get_attribute("aria-selected")) == "true"
    except Exception as e:
        logger.debug(f"UI tab '{panel_id}' error: {e}")
        return False

# ── Finalize Player Dict ─────────────────────────────────────────────────────
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
        record["gp_gs"] = str(p.get("gp_gs") or "0-0")
        record["ga"] = clean_stat_int(p.get("ga"))
        record["gaa"] = str(p.get("gaa") or "0.00")
        record["sv"] = clean_stat_int(p.get("sv"))
        record["sv_pct"] = clean_stat_pct(p.get("sv_pct"))
        record["sho"] = clean_stat_int(p.get("sho"))
        record["w_l_t"] = clean_w_l_t(p.get("w_l_t") or "0-0-0")
        record["min"] = str(p.get("min") or "0:00")
    else:
        pn_pim = p.get("pn_pim", "")
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

# ── Scrape Single Team Pipeline (Playwright DOM Driven) ──────────────────────
async def scrape_single_team(context: BrowserContext, team_cfg: Dict[str, Any], semaphore: asyncio.Semaphore) -> Dict[str, Any]:
    async with semaphore:
        slug, label = team_cfg["slug"], team_cfg["name"]
        logger.info(f"Processing {label} (Men's Hockey)...")

        roster_lookup = await fetch_roster(context, team_cfg.get("roster_url", ""), label)

        raw_players: List[Dict[str, Any]] = []
        team_stats: Dict[str, str] = {}
        style = "HTML tables"

        page: Optional[Page] = None
        try:
            page = await context.new_page()
            await page.goto(team_cfg["stats_url"], wait_until="domcontentloaded", timeout=30000)
            try:
                await page.wait_for_load_state("networkidle", timeout=4000)
            except Exception:
                pass

            # 1. Parse Skaters Table (Default Active View: Player Stats -> Overall -> Skaters)
            html_skaters = await page.content()
            soup_skaters = BeautifulSoup(html_skaters, "html.parser")
            skaters_list = parse_skaters_table(soup_skaters)
            raw_players.extend(skaters_list)

            # 2. Switch Dropdown to Goaltenders (New Style Vue/Nuxt)
            try:
                select_box = page.locator('button[data-test-id="s-select__box"]').filter(has_text=re.compile(r"Skaters|Goaltenders", re.I)).first
                if await select_box.count() > 0:
                    await select_box.click()
                    await page.wait_for_timeout(400)
                    goalie_option = page.locator('li[role="option"]').filter(has_text=re.compile(r"^Goaltenders", re.I)).first
                    if await goalie_option.count() > 0:
                        await goalie_option.click()
                        await page.wait_for_timeout(1200)
                        html_goalies = await page.content()
                        soup_goalies = BeautifulSoup(html_goalies, "html.parser")
                        goalies_list = parse_goalies_table(soup_goalies)
                        raw_players.extend(goalies_list)
                        style = "New Style (Vue dropdowns)"
            except Exception as e:
                logger.debug(f"[{label}] Goaltenders dropdown interaction: {e}")

            # If no goalies were found from dropdown, check initial page (Old Style)
            if not any(p.get("is_goalie") for p in raw_players):
                goalies_list = parse_goalies_table(soup_skaters)
                raw_players.extend(goalies_list)

            # 3. Navigate to Team Stats Tab (New Style)
            try:
                team_stats_tab = page.locator('button[data-test-id="s-tab__link"], button[role="tab"]').filter(has_text=re.compile(r"Team Stats", re.I)).first
                if await team_stats_tab.count() > 0:
                    await team_stats_tab.click()
                    await page.wait_for_timeout(1200)
                    html_team = await page.content()
                    soup_team = BeautifulSoup(html_team, "html.parser")
                    parsed_ts = parse_team_stats(soup_team)
                    if any(v != "-" for v in parsed_ts.values()):
                        team_stats = parsed_ts
            except Exception as e:
                logger.debug(f"[{label}] Team Stats tab navigation: {e}")

            # Fallback to initial page soup if Team Stats tab was not clicked
            if not team_stats or all(v == "-" for v in team_stats.values()):
                team_stats = parse_team_stats(soup_skaters)

        except Exception as e:
            logger.error(f"[{label}] Error navigating stats page: {e}")
        finally:
            if page:
                await page.close()

        # Strict Roster Merge and Player Finalization
        merged: List[Dict[str, str]] = []
        seen = set()

        for sp in raw_players:
            name = sp.get("name", "")
            if not name or name.lower() in seen or not is_valid_player_candidate(name):
                continue

            r_info = find_roster_entry(name, sp.get("number", ""), roster_lookup)

            if roster_lookup and len(roster_lookup) >= MIN_ROSTER_ENTRIES and not r_info:
                continue

            seen.add(name.lower())
            p = dict(sp)
            if r_info:
                p["name"] = r_info.get("name") or name
                p["number"] = sp.get("number") or r_info.get("number", "")
                p["position"] = "G" if sp.get("is_goalie") else (r_info.get("position") or sp.get("position", "F"))
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
            "source_roster_url": team_cfg.get("roster_url", ""),
            "site_style": style,
            "team_stats": team_stats,
            "players": merged,
        }

        # Exclusively write to docs/<slug>.json
        os.makedirs("docs", exist_ok=True)
        doc_path = os.path.join("docs", f"{slug}.json")
        with open(doc_path, "w", encoding="utf-8") as f:
            json.dump(team_data, f, indent=2)

        return team_data

# ── Scrape Single Team Pipeline: OLD STYLE (jQuery UI tabs) ──────────────────
async def scrape_single_team_old_style(context: BrowserContext, team_cfg: Dict[str, Any], semaphore: asyncio.Semaphore) -> Dict[str, Any]:
    async with semaphore:
        slug, label = team_cfg["slug"], team_cfg["name"]
        logger.info(f"Processing {label} (Men's Hockey, old style)...")

        roster_lookup = await fetch_roster(context, team_cfg.get("roster_url", ""), label)

        raw_players: List[Dict[str, Any]] = []
        team_stats: Dict[str, str] = {k: "-" for k in TEAM_STAT_KEYS}

        page: Optional[Page] = None
        try:
            page = await context.new_page()
            await page.goto(team_cfg["stats_url"], wait_until="domcontentloaded", timeout=30000)
            try:
                await page.wait_for_load_state("networkidle", timeout=4000)
            except Exception:
                pass

            # 1. Team tab: confirm <li role="tab" aria-controls="team" aria-selected="true">
            if not await _ensure_ui_tab(page, "team"):
                logger.warning(f"[{label}] Could not confirm Team tab is selected; parsing DOM anyway.")
            soup_team = BeautifulSoup(await page.content(), "html.parser")
            team_stats = parse_team_stats_old(soup_team)

            # 2. Individual -> Overall -> Skaters
            await _ensure_ui_tab(page, "individual")
            await _ensure_ui_tab(page, "individual-overall")
            await _ensure_ui_tab(page, "individual-overall-skaters")
            soup_sk = BeautifulSoup(await page.content(), "html.parser")
            skaters = parse_skaters_table_grouped(soup_sk)

            # 3. Individual -> Overall -> Goaltenders
            await _ensure_ui_tab(page, "individual-overall-goaltenders")
            html_gk = await page.content()
            soup_gk = BeautifulSoup(html_gk, "html.parser")
            goalies = parse_goalies_table_grouped(soup_gk)

            # Goalies also appear (zero-filled) in the Skaters grid, so goalies go FIRST
            # and win the name de-duplication in the merge step below.
            raw_players.extend(goalies)
            raw_players.extend(skaters)

            if not goalies:
                logger.warning(f"[{label}] No goalies parsed from Goaltenders table.")
            if all(v == "-" for v in team_stats.values()):
                logger.warning(f"[{label}] No team stats parsed. Team tab table dump:\n{team_debug_dump(soup_team)}")
                os.makedirs("debug", exist_ok=True)
                with open(os.path.join("debug", f"{slug}_team.html"), "w", encoding="utf-8") as f:
                    f.write(str(soup_team))
        except Exception as e:
            logger.error(f"[{label}] Error navigating stats page: {e}")
        finally:
            if page:
                await page.close()

        # Roster merge + finalize (same rules as new style)
        merged: List[Dict[str, str]] = []
        seen = set()
        for sp in raw_players:
            name = sp.get("name", "")
            if not name or name.lower() in seen or not is_valid_player_candidate(name):
                continue
            r_info = find_roster_entry(name, sp.get("number", ""), roster_lookup)
            if roster_lookup and len(roster_lookup) >= MIN_ROSTER_ENTRIES and not r_info:
                continue
            seen.add(name.lower())
            p = dict(sp)
            if r_info:
                p["name"] = r_info.get("name") or name
                p["number"] = sp.get("number") or r_info.get("number", "")
                p["position"] = "G" if sp.get("is_goalie") else (r_info.get("position") or sp.get("position", "F"))
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
            "source_roster_url": team_cfg.get("roster_url", ""),
            "site_style": "Old Style (jQuery UI tabs)",
            "team_stats": team_stats,
            "players": merged,
        }

        os.makedirs("docs", exist_ok=True)
        with open(os.path.join("docs", f"{slug}.json"), "w", encoding="utf-8") as f:
            json.dump(team_data, f, indent=2)
        return team_data

async def scrape_team(context: BrowserContext, team_cfg: Dict[str, Any], semaphore: asyncio.Semaphore) -> Dict[str, Any]:
    """Route each team to its pipeline. New-style pipeline is unchanged."""
    if team_cfg.get("style") == "old":
        return await scrape_single_team_old_style(context, team_cfg, semaphore)
    return await scrape_single_team(context, team_cfg, semaphore)

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
            *[scrape_team(context, t, semaphore) for t in teams],
            return_exceptions=True,
        )
        await browser.close()

    for t, res in zip(teams, results):
        if isinstance(res, Exception):
            logger.error(f"[{t['name']}] scrape failed: {res!r}")

    logger.info(f"Finished in {time.time() - start:.1f}s")

def main() -> None:
    ap = argparse.ArgumentParser(description="NCHC Men's Hockey stats scraper")
    ap.add_argument("--only", help='Comma-separated team names/slugs, e.g. "St. Thomas,Denver"')
    ap.add_argument("--debug", action="store_true", help="Debug mode")
    args = ap.parse_args()
    asyncio.run(run_scraper(args.only.split(",") if args.only else None, args.debug))

if __name__ == "__main__":
    main()
