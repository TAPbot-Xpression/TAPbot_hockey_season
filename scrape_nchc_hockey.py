#!/usr/bin/env python3
"""
scrape_nchc_hockey.py
NCHC Men's Hockey player season stats + team stats scraper.

Key Updates:
1. Goaltender Record Parsing:
   - Accurately captures separate 'W', 'L', and 'T' columns from the Goaltender stats table
     and compiles them into 'W-L-T' (e.g., 3-3-0 or 7-2-1).
   - Also supports composite 'W-L-T' / 'RECORD' columns if present.
2. Output Directory:
   - Exclusively writes JSON files to the 'docs/' directory (docs/<slug>.json).
   - No longer generates redundant JSON files in the root directory.
3. Non-Conference Standardization:
   - Guarantees any non-conference team defined in non_conf_config.json is saved as
     'docs/non_conf_mhockey.json' for seamless integration with index.html.
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
    # Match standard format like 3-3-0 or 7-2
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
                    # Strictly ensure slug is non_conf_mhockey for web dashboard
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

        # Parse table rows (Print Roster)
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

        # Parse sidearm card items (List View)
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
    # Separate W, L, T columns
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

            # GP-GS formatting
            gp_val = cells[mapping["gp"]] if "gp" in mapping and mapping["gp"] < len(cells) else "0"
            gs_val = cells[mapping["gs"]] if "gs" in mapping and mapping["gs"] < len(cells) else "0"
            gp_gs_str = cells[mapping["gp_gs"]] if "gp_gs" in mapping and mapping["gp_gs"] < len(cells) else f"{gp_val}-{gs_val}"

            # W-L-T record formatting from individual W, L, T columns or composite column
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

            # 1. Parse Skaters Table (Default Active View)
            html_skaters = await page.content()
            soup_skaters = BeautifulSoup(html_skaters, "html.parser")
            skaters_list = parse_skaters_table(soup_skaters)
            raw_players.extend(skaters_list)

            # 2. Parse Team Stats
            team_stats = parse_team_stats(soup_skaters)

            # 3. Switch Dropdown to Goaltenders (New Style Vue/Nuxt)
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

            # If no goalies were found from dropdown, parse from initial page (Old Style)
            if not any(p.get("is_goalie") for p in raw_players):
                goalies_list = parse_goalies_table(soup_skaters)
                raw_players.extend(goalies_list)

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

            # Strict Roster Gate: Discard unrostered match splits or label artifacts
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
