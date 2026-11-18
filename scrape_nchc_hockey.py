#!/usr/bin/env python3
"""
scrape_nchc_hockey.py
NCHC Men's Hockey player season stats + team stats scraper (Sidearm sites).
Built from scrape_basketball.py (same Playwright + BeautifulSoup approach, same
roster-merge logic, same "keep previous JSON if a scrape comes back empty" behaviour).

JSON layout (identical for every team file):
    {
      "team": "St. Thomas",
      "slug": "st_thomas_mhockey",
      "gender": "men",
      "sport": "hockey",
      "conference": "NCHC",
      "generated_at": "...",
      "source_stats_url": "...",
      "source_roster_url": "...",
      "site_style": "New Style (Nuxt data)" | "Old Style (HTML tables)" | "Arizona State (custom)" | "none",
      "team_stats_status": "ok" | "partial" | "kept_previous" | "not_found",
      "team_stats_missing": [],
      "team_stats": {
          "goals_per_game", "shots_per_game", "shot_pct", "pp_pct", "pk_pct",
          "pen_per_game", "pim_per_game", "faceoff_pct"
      },
      "players": [
          {"number", "name", "position" (G/D/F), "year",
           "gp", "g", "a", "pts", "sh", "blk", "ppg", "shg", "gwg", "pn_pim"}
      ]
    }

Notes
  - PK% follows the definition in the request: the OPPONENT'S power play conversion %.
    Set PK_PCT_IS_OPP_PP = False to output a true penalty-kill % (100 - opp PP%) instead.
  - If a team's page has no team-stats block, per-game numbers are derived from the
    "Total" row of the player table (goals, shots, PN-PIM, faceoffs) when possible.
  - Run with --debug to save the raw HTML of every page to ./debug/ (handy for fixing
    Arizona State or any site whose layout differs from what the parsers expect).
  - Run with --only "St. Thomas,Denver" to scrape a subset.
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
MIN_ROSTER_ENTRIES = 12      # roster lookup holds 2 keys per player (name and #number)
MIN_PLAYERS_ACCEPT = 5       # a stats strategy "wins" once it finds this many players
PK_PCT_IS_OPP_PP = True      # True = PK% field holds opponent PP% (as requested)

# ── NCHC Men's Hockey Configuration ───────────────────────────────────────────
# style: "new" (Nuxt data first), "old" (HTML tables first), "asu" (custom parser first).
# Whatever the hint, the other strategies are tried automatically if the first finds nothing.
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
        "name": "Omaha", "slug": "omaha_mhockey", "style": "new",   # style not labeled in request; auto-fallback covers it
        "stats_url": "https://omahamavs.com/sports/mens-ice-hockey/stats/2026-27",
        "roster_url": "https://omahamavs.com/sports/mens-ice-hockey/roster/print",
    },
]

PLAYER_STAT_FIELDS = ["gp", "g", "a", "pts", "sh", "blk", "ppg", "shg", "gwg", "pn_pim"]
INT_FIELDS = [f for f in PLAYER_STAT_FIELDS if f != "pn_pim"]
TEAM_STAT_KEYS = [
    "goals_per_game", "shots_per_game", "shot_pct", "pp_pct",
    "pk_pct", "pen_per_game", "pim_per_game", "faceoff_pct",
]

DISCARD_EXACT_TOKENS = {
    "ft%", "blk", "ast", "tot", "avg", "pts", "off", "def", "min", "stl",
    "gp", "gs", "pct", "reb", "player", "no.", "total", "totals", "team",
    "opponent", "opponents", "view bio", "bio", "team tm team", "teamtmteam",
    "tm", "#tm", "tm team", "#tm team", "overall", "conference", "image",
    "full name", "name", "scoring", "averages", "minutes", "player name",
    "title", "yr.", "bio link", "statistic", "high", "opponent(s)", "opp",
    "//", "date", "score", "unassigned", "pos.", "ht.", "wt.", "cl.",
    "hometown", "high school", "previous school", "major",
    # hockey headers / section labels
    "g", "a", "sh", "sh%", "ppg", "shg", "gwg", "pn-pim", "pim", "gaa", "sv%",
    "skaters", "goaltenders", "goalies", "goalkeepers", "plus/minus", "+/-",
    "goaltending", "penalties", "power play", "short handed",
}

STAFF_KEYWORDS = [
    "coach", "trainer", "director", "coordinator", "manager", "athletic",
    "operations", "strength", "conditioning", "physician", "assistant",
    "communications", "compliance", "academic", "marketing",
]

_SUFFIX_SET = {"jr", "jr.", "sr", "sr.", "ii", "iii", "iv", "v"}
TOTAL_LABELS = {"total", "totals", "teamtotals", "team", "overall", "overalltotals", "teamtotal"}

# ── Generic helpers ───────────────────────────────────────────────────────────
_NUM_RE = re.compile(r"^[+\-]?\d*\.?\d+%?$")


def _num_text(s: Any) -> Optional[str]:
    if s is None:
        return None
    t = str(s).strip().replace("\u2212", "-").replace("\u2013", "-").replace(",", "")
    return t if t and _NUM_RE.match(t) else None


def _f(x: Any) -> Optional[float]:
    t = _num_text(x) if x not in (None, "") else None
    if t is None:
        return None
    try:
        return float(t.rstrip("%"))
    except Exception:
        return None


def _label_key(text: str) -> str:
    return re.sub(r"[^a-z0-9%/:]", "", (text or "").lower())


def _hk(text: str) -> str:
    """Header key: uppercase, keep letters/digits and % / + -."""
    t = (text or "").upper().replace("\u2013", "-").replace("\u2014", "-")
    return re.sub(r"[^A-Z0-9%/+\-]", "", t)


def _nk(text: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(text).lower())


def _alpha(text: str) -> str:
    return re.sub(r"[^a-z]", "", (text or "").lower())


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ── Fetchers ──────────────────────────────────────────────────────────────────
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


def derive_roster_candidates(stats_url: str) -> List[str]:
    base = stats_url.strip().split("#")[0].split("?")[0].rstrip("/")
    m = re.match(r"^(.*?)/stats(?:/(\d{4}-\d{2,4}))?$", base)
    if not m:
        return []
    root, season = m.group(1), m.group(2)
    cands: List[str] = []
    if season:
        cands += [f"{root}/roster/print/{season}", f"{root}/roster/{season}?print=true"]
    cands += [f"{root}/roster/print", f"{root}/roster?print=true"]
    if season:
        cands.append(f"{root}/roster/{season}")
    cands.append(f"{root}/roster")
    seen, unique = set(), []
    for c in cands:
        if c not in seen:
            seen.add(c)
            unique.append(c)
    return unique


# ── Cleaning helpers ──────────────────────────────────────────────────────────
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


def clean_pn_pim(val: Any) -> str:
    """Keeps 'penalties-minutes' format, e.g. '12-24'. Empty -> '0-0'."""
    if val is None:
        return "0-0"
    s = str(val).strip().replace("\u2013", "-").replace("\u2014", "-")
    m = re.match(r"^(\d+)\s*[-/]\s*(\d+)$", s)
    if m:
        return f"{int(m.group(1))}-{int(m.group(2))}"
    if re.match(r"^\d+$", s):
        return f"0-{int(s)}"
    return "0-0"


def clean_name(raw_name: str) -> str:
    """
    Cleans player names and keeps suffixes (Jr., Sr., III, ...) at the end.
    'Williams, Jr., Fontaine' -> 'Fontaine Williams, Jr.'
    """
    if not raw_name:
        return ""

    text = raw_name.replace("View Bio", "").replace("Image", "").strip()
    text = re.sub(r"['\"]+", "", text).strip()
    if "@" in text or not text:
        return ""

    text = re.sub(r"#\d{1,2}\b", " ", text)
    text = re.sub(r"#?tm\b", " ", text, flags=re.I)

    dup_m = re.search(r"^(.*?)(?:\d{1,2})?\1$", text)
    if dup_m and len(dup_m.group(1)) > 3:
        text = dup_m.group(1)

    text = text.strip().rstrip(",").strip()

    if "," in text:
        raw_parts = [re.sub(r"\b\d{1,2}\b", "", p).strip() for p in text.split(",") if p.strip()]
        last_name = raw_parts[0]
        suffix = ""
        non_suffix = []
        for p in raw_parts[1:]:
            if p.lower() in _SUFFIX_SET or re.sub(r"[^a-z]", "", p.lower()) in {"jr", "sr", "ii", "iii", "iv", "v"}:
                suffix = p
            else:
                non_suffix.append(p)
        first_name = " ".join(non_suffix).strip()

        if first_name and last_name:
            if suffix:
                clean_suf = suffix if suffix.endswith(".") or suffix.upper() in {"II", "III", "IV", "V"} else f"{suffix}."
                text = f"{first_name} {last_name}, {clean_suf.title() if clean_suf.lower().startswith(('j', 's')) else clean_suf.upper()}"
            else:
                text = f"{first_name} {last_name}"
        else:
            text = first_name or last_name

    text = re.sub(r"\b\d{1,2}\b", "", text)
    text = " ".join(text.split()).strip()

    words = text.split()
    half = len(words) // 2
    if len(words) >= 4 and half * 2 == len(words) and words[:half] == words[half:]:
        text = " ".join(words[:half])
    return text


def clean_number(raw_num: Any) -> str:
    if raw_num is None:
        return ""
    text = str(raw_num).strip().replace("#", "")
    if text.upper() in ("TM", "TEAM", "TOT", "TOTAL"):
        return ""
    m = re.match(r"^(\d{1,2})$", text)
    return str(int(m.group(1))) if m else ""


# Goalie = G, Defense = D, Forward = F (centers and wingers roll up into F)
_POS_MAP = {
    "G": "G", "GK": "G", "GOALIE": "G", "GOALTENDER": "G", "GOALKEEPER": "G", "GOALTENDERS": "G",
    "D": "D", "DEF": "D", "DEFENSE": "D", "DEFENSEMAN": "D", "DEFENSEMEN": "D", "DEFENCE": "D",
    "DEFENCEMAN": "D", "DMAN": "D", "LD": "D", "RD": "D",
    "F": "F", "FWD": "F", "FORWARD": "F", "C": "F", "CENTER": "F", "CENTRE": "F",
    "LW": "F", "RW": "F", "W": "F", "WING": "F", "WINGER": "F", "LEFTWING": "F", "RIGHTWING": "F",
}


def normalize_position(pos_str: str) -> str:
    if not pos_str:
        return ""
    p = re.sub(r"[^A-Z/]", "", str(pos_str).upper())
    for token in p.split("/"):
        if token in _POS_MAP:
            return _POS_MAP[token]
    return ""


_CLASS_MAP = {
    "FR": "FR", "FRESHMAN": "FR", "FY": "FR", "FIRST": "FR", "FIRSTYEAR": "FR", "1ST": "FR", "1STYEAR": "FR",
    "SO": "SO", "SOPHOMORE": "SO", "SECOND": "SO", "SECONDYEAR": "SO", "2ND": "SO", "2NDYEAR": "SO",
    "JR": "JR", "JUNIOR": "JR", "THIRD": "JR", "THIRDYEAR": "JR", "3RD": "JR", "3RDYEAR": "JR",
    "SR": "SR", "SENIOR": "SR", "FOURTH": "SR", "FOURTHYEAR": "SR", "4TH": "SR", "4THYEAR": "SR",
    "GR": "GR", "GRAD": "GR", "GRADUATE": "GR", "GRADUATESTUDENT": "GR", "GRADSTUDENT": "GR", "GS": "GR",
    "FIFTH": "GR", "FIFTHYEAR": "GR", "5TH": "GR", "5THYEAR": "GR",
}


def normalize_year(yr_str: str) -> str:
    if not yr_str:
        return ""
    y = re.sub(r"[^A-Z0-9]", "", str(yr_str).upper())
    if not y:
        return ""
    if y in _CLASS_MAP:
        return _CLASS_MAP[y]
    for prefix in ("REDSHIRT", "RS", "R"):
        if y.startswith(prefix):
            remainder = y[len(prefix):]
            if remainder in _CLASS_MAP:
                base = _CLASS_MAP[remainder]
                return f"R-{base}" if base != "GR" else "GR"
    return ""


def is_valid_player_name(name: str) -> bool:
    if not name or len(name.strip()) < 2:
        return False
    raw_low = name.strip().lower()
    condensed = re.sub(r"[\s\-_#]+", "", raw_low)
    if raw_low in DISCARD_EXACT_TOKENS or condensed in DISCARD_EXACT_TOKENS:
        return False
    if any(tok in condensed for tok in ["teamtm", "teamteam", "tmteam", "total", "opponent", "opponents", "gamebygame", "comparison"]):
        return False
    if condensed in ("team", "tm", "totals", "total"):
        return False
    if raw_low.startswith("#tm") or raw_low.startswith("tm "):
        return False
    if re.match(r"^(\.\d+|\d+(\.\d+)?|\d{1,2}/\d{1,2}/\d{2,4})$", raw_low):
        return False
    if re.search(r"\b(vs|at|\d+-\d+)\b", raw_low):
        return False
    if any(staff in raw_low for staff in STAFF_KEYWORDS):
        return False
    if raw_low.startswith("//") or raw_low in ("/", "null"):
        return False
    return True


def strip_suffixes(name: str) -> str:
    cleaned = re.sub(r"\b(jr\.?|sr\.?|iii|iv|ii)\b", "", name.lower()).strip()
    return re.sub(r"\s+", " ", cleaned)


def find_roster_entry(name: str, number: str, roster_lookup: Dict[str, Dict[str, str]]) -> Optional[Dict[str, str]]:
    if not roster_lookup:
        return None
    raw_low = name.lower().strip()

    if raw_low in roster_lookup:                                   # Tier 1: exact name
        return roster_lookup[raw_low]
    if number and f"#{number}" in roster_lookup:                   # Tier 2: jersey number
        return roster_lookup[f"#{number}"]

    base_name = strip_suffixes(raw_low)                            # Tier 3: suffix-agnostic
    for r_key, r_info in roster_lookup.items():
        if not r_key.startswith("#") and strip_suffixes(r_key) == base_name:
            return r_info

    name_parts = raw_low.split()                                   # Tier 4: last name (+ number)
    if name_parts:
        last_name = name_parts[-1]
        for r_key, r_info in roster_lookup.items():
            if r_key.startswith("#"):
                continue
            r_parts = r_key.split()
            if r_parts and r_parts[-1] == last_name and (not number or r_info.get("number") == number):
                return r_info

    roster_names = [k for k in roster_lookup if not k.startswith("#")]   # Tier 5: fuzzy
    close = difflib.get_close_matches(raw_low, roster_names, n=1, cutoff=0.72)
    return roster_lookup[close[0]] if close else None


# ── Roster parsing ────────────────────────────────────────────────────────────
NUXT_PATTERNS = [
    r'<script[^>]+id=["\']__NUXT_DATA__["\'][^>]*>(.*?)</script>',
    r"__NUXT_DATA__\s*=\s*(\[.*?\]);",
    r"window\.__NUXT__\s*=\s*(.*?);\s*</script>",
]


def _add_roster(lookup: Dict[str, Dict[str, str]], name: str, num: str, pos: str, yr: str) -> None:
    info = {"name": name, "number": num, "position": pos, "year": yr}
    lookup[name.lower()] = info
    if num != "":
        lookup[f"#{num}"] = info


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
                raw_name = (item.get("name") or item.get("playerName") or item.get("fullName") or
                            item.get("displayName") or item.get("nameFromStats"))
                if not raw_name:
                    continue
                c_name = clean_name(resolve(raw_name))
                if not is_valid_player_name(c_name):
                    continue
                raw_num = (item.get("uniform") or item.get("jersey") or item.get("playerUniform") or
                           item.get("number") or item.get("jerseyNumber"))
                raw_pos = (item.get("position") or item.get("positionShort") or item.get("pos") or
                           item.get("positionName") or item.get("player_position") or item.get("positionLong"))
                raw_yr = (item.get("academicYear") or item.get("year") or item.get("class") or
                          item.get("academicClass") or item.get("classYear") or item.get("cl") or
                          item.get("yearShort") or item.get("academicYearShort") or item.get("academicStanding"))
                _add_roster(roster_lookup, c_name, clean_number(resolve(raw_num)),
                            normalize_position(resolve(raw_pos)), normalize_year(resolve(raw_yr)))
        except Exception:
            continue
        if len(roster_lookup) >= 5:
            break
    return roster_lookup


def parse_roster_cards(soup: BeautifulSoup) -> Dict[str, Dict[str, str]]:
    """Sidearm 'list view' roster cards (used by e.g. roster?view=list)."""
    lookup: Dict[str, Dict[str, str]] = {}
    for card in soup.select('[class~="sidearm-roster-player"]'):
        name_el = card.select_one('[class*="player-name"] a') or card.select_one('[class*="player-name"]')
        if not name_el:
            continue
        name = clean_name(name_el.get_text(" ", strip=True))
        if not is_valid_player_name(name):
            continue
        jersey_el = card.select_one('[class*="jersey-number"]')
        num = clean_number(jersey_el.get_text(" ", strip=True)) if jersey_el else ""
        pos_el = card.select_one('[class*="player-position"]') or card.select_one('[class*="position"]')
        pos = ""
        if pos_el:
            for tok in pos_el.get_text(" ", strip=True).split():
                pos = normalize_position(tok)
                if pos:
                    break
        yr_el = card.select_one('[class*="academic-year"]')
        yr = normalize_year(yr_el.get_text(" ", strip=True)) if yr_el else ""
        _add_roster(lookup, name, num, pos, yr)
    return lookup


def parse_print_roster(html: str) -> Dict[str, Dict[str, str]]:
    roster_lookup = parse_nuxt_roster_data(html)
    if len(roster_lookup) >= 5:
        return roster_lookup

    soup = BeautifulSoup(html, "html.parser")

    cards = parse_roster_cards(soup)
    if len(cards) >= 5:
        return cards

    for staff in soup.find_all(class_=re.compile(r"staff|coaches|coach", re.I)):
        staff.decompose()
    for hdr in soup.find_all(["h2", "h3", "h4"], string=re.compile(r"coaching staff|coaches", re.I)):
        parent = hdr.find_parent(["section", "div", "table"])
        if parent:
            parent.decompose()

    for row in soup.find_all("tr"):
        cells = [c.get_text(strip=True) for c in row.find_all(["td", "th"])]
        if len(cells) < 3:
            continue
        num, pos, yr, name_cand = "", "", "", ""
        for col in cells:
            if not num and re.match(r"^\d{1,2}$", col):
                num = clean_number(col)
                continue
            if not pos and normalize_position(col) and len(col) <= 14:
                pos = normalize_position(col)
                continue
            if not yr and normalize_year(col):
                yr = normalize_year(col)
                continue
            c_clean = clean_name(col)
            if not name_cand and is_valid_player_name(c_clean):
                name_cand = c_clean
        if name_cand and is_valid_player_name(name_cand):
            _add_roster(roster_lookup, name_cand, num, pos, yr)

    if not roster_lookup:
        lines = [ln.strip() for ln in soup.get_text(separator="\n", strip=True).split("\n") if ln.strip()]
        i = 0
        while i < len(lines):
            if re.match(r"^\d{1,2}$", lines[i]):
                p_num = clean_number(lines[i])
                p_name, p_pos, p_yr = "", "", ""
                j = i + 1
                limit = min(len(lines), i + 8)
                while j < limit:
                    nxt = lines[j]
                    if re.match(r"^\d{1,2}$", nxt) and j > i + 1:
                        break
                    if not p_pos and normalize_position(nxt) and len(nxt) <= 14:
                        p_pos = normalize_position(nxt)
                    elif not p_yr and normalize_year(nxt):
                        p_yr = normalize_year(nxt)
                    elif not p_name and is_valid_player_name(clean_name(nxt)):
                        p_name = clean_name(nxt)
                    j += 1
                if p_name:
                    _add_roster(roster_lookup, p_name, p_num, p_pos, p_yr)
                i = j - 1
            i += 1

    return roster_lookup


async def fetch_roster(context: BrowserContext, candidates: List[str], label: str,
                       debug_dir: Optional[str], slug: str) -> Tuple[Dict[str, Dict[str, str]], str]:
    best_lookup: Dict[str, Dict[str, str]] = {}
    best_url = ""
    for n, url in enumerate(candidates):
        html = await fetch_page_html(context, url)
        if not html:
            continue
        if debug_dir:
            _dump(debug_dir, f"{slug}_roster{n}.html", html)
        lookup = parse_print_roster(html)
        if len(lookup) > len(best_lookup):
            best_lookup, best_url = lookup, url
        if len(lookup) >= MIN_ROSTER_ENTRIES:
            return lookup, url
        logger.info(f"  [{label}] Roster URL gave {len(lookup)} entries, trying next option: {url}")
    return best_lookup, best_url


# ── Player stat parsing: HTML tables (old style, ASU, new-style fallback) ─────
HEADER_ALIASES: Dict[str, List[str]] = {
    "gp": ["GP", "GAMES", "GAMESPLAYED", "GP-GS", "GP/GS"],
    "g": ["G", "GOALS"],
    "a": ["A", "AST", "ASSISTS"],
    "pts": ["PTS", "POINTS", "TP", "PT"],
    "sh": ["SH", "SOG", "SHOTS", "SHOTSONGOAL"],
    "blk": ["BLK", "BLKS", "BS", "BLOCKS", "BLOCKEDSHOTS", "BLKD", "SHOTSBLOCKED"],
    "ppg": ["PPG", "PP", "PPGOALS", "POWERPLAYGOALS", "POWERPLAYG", "POWERPLAYGOAL"],
    "shg": ["SHG", "SHGOALS", "SHORTHANDEDGOALS", "SHORTHANDGOALS", "SHORTHANDEDG", "SHORTHANDG"],
    "gwg": ["GWG", "GW", "GAMEWINNINGGOALS", "GWGOALS", "GAMEWINNINGG"],
    "pn_pim": ["PN-PIM", "PEN-PIM", "PEN-MIN", "PN-MIN", "PEN/PIM", "PN/PIM", "PENALTIES-PIM", "PENALTIES-MINUTES"],
    "pen": ["PEN", "PN", "PENALTIES", "PENS"],
    "pim": ["PIM", "PENALTYMINUTES", "PENMIN", "PENALTYMIN", "PENMINUTES"],
    # team-totals helpers
    "fow": ["FOW", "FACEOFFSWON"],
    "fol": ["FOL", "FACEOFFSLOST"],
    "fo_pct": ["FO%", "FOPCT", "FACEOFF%", "FACEOFFPCT", "FOW%"],
    # goalie markers
    "gaa": ["GAA", "GOALSAGAINSTAVERAGE"],
    "sv_pct": ["SV%", "SVPCT", "SAVE%", "SAVEPCT", "SVP"],
    "saves": ["SV", "SAVES"],
    "ga": ["GA", "GOALSAGAINST"],
}
HEADER_LOOKUP: Dict[str, str] = {_hk(a): f for f, lst in HEADER_ALIASES.items() for a in lst}
RECOGNIZED = set(PLAYER_STAT_FIELDS) | {"pen", "pim", "gaa", "sv_pct", "saves", "ga"}
ROW_FIELDS = PLAYER_STAT_FIELDS + ["pen", "pim", "fow", "fol", "fo_pct"]
NAME_HEADERS = {"PLAYER", "NAME", "PLAYERNAME", "FULLNAME", "ATHLETE"}
NUM_HEADERS = {"#", "no", "no.", "jersey", "num", "number", "uniform"}
SKIP_CONTEXT = ["game-by-game", "game by game", "gamebygame", "comparison", "game-highs",
                "game highs", "highs", "schedule", "exhibition", "conference"]
HARD_SKIP_CONTEXT = ["game-by-game", "game by game", "gamebygame", "comparison"]


def _expand_header_grid(rows: List[Any]) -> List[Dict[str, str]]:
    grid: Dict[Tuple[int, int], str] = {}
    for r_i, row in enumerate(rows):
        c = 0
        for cell in row.find_all(["th", "td"]):
            while (r_i, c) in grid:
                c += 1
            text = cell.get_text(" ", strip=True)
            try:
                rs = int(cell.get("rowspan", 1))
            except Exception:
                rs = 1
            try:
                cs = int(cell.get("colspan", 1))
            except Exception:
                cs = 1
            for rr in range(rs):
                for cc in range(cs):
                    grid[(r_i + rr, c + cc)] = text
            c += cs
    if not grid:
        return []
    ncols = max(c for (_, c) in grid) + 1
    nrows = len(rows)
    cols: List[Dict[str, str]] = []
    for c in range(ncols):
        texts = [grid.get((r, c), "") for r in range(nrows)]
        uniq: List[str] = []
        for t in texts:
            if t and t not in uniq:
                uniq.append(t)
        leaf = next((t for t in reversed(texts) if t), "")
        cols.append({"leaf": leaf, "combined": " ".join(uniq), "data_label": ""})
    return cols


def get_table_columns(table: Any, rows: List[Any]) -> Tuple[List[Any], List[Dict[str, str]]]:
    thead = table.find("thead")
    header_rows = thead.find_all("tr") if thead else []

    if not header_rows:                      # no <thead>: find a header-looking row near the top
        for r in rows[:4]:
            cells = r.find_all(["th", "td"])
            hits = sum(1 for c in cells if _hk(c.get_text(" ", strip=True)) in HEADER_LOOKUP)
            if hits >= 3:
                header_rows = [r]
                break

    cols = _expand_header_grid(header_rows) if header_rows else []

    # data-label attributes on body cells (Sidearm) as extra / fallback labels
    header_ids = {id(r) for r in header_rows}
    for r in rows:
        if id(r) in header_ids:
            continue
        cells = r.find_all(["td", "th"])
        labels = [c.get("data-label", "") for c in cells]
        if sum(1 for lab in labels if _hk(lab) in HEADER_LOOKUP) >= 3:
            if not cols:
                cols = [{"leaf": lab, "combined": lab, "data_label": lab} for lab in labels]
            else:
                for i, lab in enumerate(labels[:len(cols)]):
                    cols[i]["data_label"] = lab
            break
    return header_rows, cols


def map_columns(cols: List[Dict[str, str]]) -> Dict[str, int]:
    mapping: Dict[str, int] = {}
    for i, col in enumerate(cols):
        for cand in (col.get("combined"), col.get("leaf"), col.get("data_label")):
            f = HEADER_LOOKUP.get(_hk(cand or ""))
            if f:
                mapping.setdefault(f, i)
                break
    return mapping


def locate_name_num(cols: List[Dict[str, str]]) -> Tuple[Optional[int], Optional[int]]:
    name_idx = num_idx = None
    for i, col in enumerate(cols):
        leaf = (col.get("leaf") or col.get("data_label") or "").strip()
        if name_idx is None and _hk(leaf) in NAME_HEADERS:
            name_idx = i
        if num_idx is None and leaf.lower() in NUM_HEADERS:
            num_idx = i
    return name_idx, num_idx


def table_context(table: Any) -> str:
    parts: List[str] = []
    cap = table.find("caption")
    if cap:
        parts.append(cap.get_text(" ", strip=True))
    prev = table.find_previous(["h1", "h2", "h3", "h4", "h5", "caption"])
    if prev:
        parts.append(prev.get_text(" ", strip=True))
    for i, p in enumerate(table.parents):
        if i >= 4:
            break
        if hasattr(p, "get"):
            parts.append(" ".join(p.get("class", []) or []) + " " + (p.get("id") or ""))
    return " ".join(parts).lower()


def _clean_cell(v: str) -> str:
    v = (v or "").strip()
    if v.lower() in ("-", "--", "—", "null", "none", "n/a", "na", "view bio"):
        return ""
    return v


def _guess_name_cell(texts: List[str]) -> str:
    for t in texts[:4]:
        if re.search(r"[A-Za-z]{2,}", t) and not t.isdigit() and _hk(t) not in HEADER_LOOKUP:
            return t
    return ""


def _blank_player() -> Dict[str, Any]:
    p: Dict[str, Any] = {"number": "", "name": "", "position": "", "year": "", "goalie": False}
    for f in ROW_FIELDS:
        p[f] = ""
    return p


def _merge_player(store: Dict[str, Dict[str, Any]], order: List[str], p: Dict[str, Any]) -> None:
    key = p["name"].lower()
    if key not in store:
        store[key] = p
        order.append(key)
        return
    cur = store[key]
    for f in ["number"] + ROW_FIELDS:
        if not cur.get(f) and p.get(f):
            cur[f] = p[f]
    cur["goalie"] = cur["goalie"] or p["goalie"]


def _parse_stats_tables(soup: BeautifulSoup, lenient: bool, strict: bool) -> Tuple[List[Dict[str, Any]], Dict[str, str]]:
    store: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []
    totals: Dict[str, str] = {}
    need = 2 if lenient else 3

    for table in soup.find_all("table"):
        ctx = table_context(table)
        bad = SKIP_CONTEXT if (strict and not lenient) else HARD_SKIP_CONTEXT
        if any(b in ctx for b in bad):
            continue

        rows = table.find_all("tr")
        header_rows, cols = get_table_columns(table, rows)
        if not cols:
            continue
        mapping = map_columns(cols)
        if len(set(mapping) & RECOGNIZED) < need:
            continue
        leaf_set = {_hk(c.get("leaf", "")) for c in cols}
        if leaf_set & {"OPPONENT", "DATE", "RESULT", "SCORE", "OPP"}:
            continue

        name_idx, num_idx = locate_name_num(cols)
        goalie_table = bool({"gaa", "sv_pct"} & set(mapping)) or ("saves" in mapping and "g" not in mapping)
        header_ids = {id(r) for r in header_rows}

        for row in rows:
            if id(row) in header_ids:
                continue
            if row.find("th") and not row.find("td"):
                continue
            cells = row.find_all(["td", "th"])
            if len(cells) < 3:
                continue
            texts = [c.get_text(" ", strip=True) for c in cells]

            raw_name = ""
            for a in row.find_all("a", href=re.compile(r"roster", re.I)) + row.find_all("a", attrs={"data-player-id": True}):
                t = a.get_text(" ", strip=True)
                if t and "view bio" not in t.lower():
                    raw_name = t
                    break
            if not raw_name and name_idx is not None and name_idx < len(texts):
                raw_name = texts[name_idx]
            if not raw_name:
                raw_name = _guess_name_cell(texts)

            fields = {f: _clean_cell(texts[mapping[f]]) for f in ROW_FIELDS if f in mapping and mapping[f] < len(texts)}

            # Team "Total" row -> kept separately (used to derive team stats)
            if any(_alpha(t) in TOTAL_LABELS for t in texts[:3]) and _alpha(raw_name) in TOTAL_LABELS | {""}:
                if not totals and any(fields.values()):
                    totals = {k: v for k, v in fields.items() if v}
                continue
            if any(_alpha(t) in {"opponent", "opponents", "opp"} for t in texts[:3]):
                continue

            c_name = clean_name(raw_name)
            if not is_valid_player_name(c_name):
                continue
            if _alpha(c_name) in TOTAL_LABELS:
                continue
            if not any(fields.values()):
                continue

            raw_num = ""
            if num_idx is not None and num_idx < len(texts):
                raw_num = texts[num_idx]
            elif texts[0].isdigit():
                raw_num = texts[0]
            elif lenient:
                m = re.match(r"^\s*#?(\d{1,2})\b", raw_name)
                raw_num = m.group(1) if m else ""

            p = _blank_player()
            p.update(fields)
            p["number"] = clean_number(raw_num)
            p["name"] = c_name
            p["goalie"] = goalie_table
            _merge_player(store, order, p)

    return [store[k] for k in order], totals


def parse_stats_tables(soup: BeautifulSoup, lenient: bool = False) -> Tuple[List[Dict[str, Any]], Dict[str, str]]:
    for strict in (True, False):       # second pass drops the context filter in case it was too aggressive
        players, totals = _parse_stats_tables(soup, lenient, strict)
        if players:
            return players, totals
    return [], {}


# ── Player stat parsing: new style (Nuxt data) ────────────────────────────────
NUXT_ALIASES: Dict[str, List[str]] = {
    "gp": ["gamesPlayed", "gp", "games", "gpgs"],
    "g": ["goals", "g", "totalGoals"],
    "a": ["assists", "a", "totalAssists"],
    "pts": ["points", "pts", "totalPoints"],
    "sh": ["shots", "sh", "sog", "shotsOnGoal", "totalShots"],
    "blk": ["blockedShots", "blocks", "blk", "blks", "shotsBlocked", "blockShots"],
    "ppg": ["powerPlayGoals", "ppg", "ppGoals", "powerplayGoals"],
    "shg": ["shortHandedGoals", "shg", "shGoals", "shorthandedGoals"],
    "gwg": ["gameWinningGoals", "gwg", "gameWinners"],
    "pn_pim": ["pnPim", "penPim", "penaltiesPim", "pnpim", "penaltiesPenaltyMinutes"],
    "pen": ["penalties", "pen", "pn", "penaltyCount", "numPenalties"],
    "pim": ["penaltyMinutes", "pim", "penMin", "penaltyMins", "penaltyMin"],
    "fow": ["faceoffsWon", "fow", "faceOffsWon"],
    "fol": ["faceoffsLost", "fol", "faceOffsLost"],
}
NUXT_LOOKUP = {f: [_nk(a) for a in lst] for f, lst in NUXT_ALIASES.items()}
GOALIE_KEYS = ["savepercentage", "savepct", "goalsagainstaverage", "gaa", "saves", "goalsagainst"]


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

            def resolve(val: Any) -> str:
                if isinstance(val, int) and 0 <= val < len(data):
                    target = data[val]
                    if isinstance(target, (str, int, float)):
                        return str(target)
                return str(val) if val is not None else ""

            for item in data:
                if not isinstance(item, dict):
                    continue
                if "opponent" in item or "winOrLoss" in item or "boxscoreUrl" in item:
                    continue
                norm = {_nk(k): v for k, v in item.items()}

                def stat(field: str) -> str:
                    for a in NUXT_LOOKUP[field]:
                        if a in norm:
                            res = resolve(norm[a]).strip()
                            if res and res not in ("None", "null"):
                                return res
                    return ""

                fields = {f: stat(f) for f in ROW_FIELDS if f in NUXT_LOOKUP}
                if not any(fields.get(f) for f in ("gp", "g", "a", "pts", "sh")):
                    continue

                if item.get("isAFooterStat") is True:
                    if not totals:
                        totals = {k: v for k, v in fields.items() if v}
                    continue

                raw_name = None
                for k in ("playername", "namefromstats", "name", "fullname", "displayname"):
                    if k in norm and norm[k] is not None:
                        raw_name = norm[k]
                        break
                if raw_name is None:
                    continue
                c_name = clean_name(resolve(raw_name))
                if _alpha(c_name) in TOTAL_LABELS:
                    if not totals:
                        totals = {k: v for k, v in fields.items() if v}
                    continue
                if not is_valid_player_name(c_name):
                    continue

                raw_num = None
                for k in ("playeruniform", "jersey", "number", "uniform", "jerseynumber"):
                    if k in norm:
                        raw_num = norm[k]
                        break
                raw_pos = ""
                for k in ("position", "pos", "positionshort", "positionlong"):
                    if k in norm:
                        raw_pos = resolve(norm[k])
                        break
                raw_yr = ""
                for k in ("academicyear", "year", "class", "academicclass", "classyear", "yearshort"):
                    if k in norm:
                        raw_yr = resolve(norm[k])
                        break

                p = _blank_player()
                p.update(fields)
                p["number"] = clean_number(resolve(raw_num))
                p["name"] = c_name
                p["position"] = normalize_position(raw_pos)
                p["year"] = normalize_year(raw_yr)
                p["goalie"] = any(k in norm for k in GOALIE_KEYS)
                players.append(p)
        except Exception:
            continue
        if len(players) >= 5:
            break

    return players, totals


# ── Team stats ────────────────────────────────────────────────────────────────
TEAM_LABEL_ALIASES: Dict[str, List[str]] = {
    "goals_per_game": ["Goals Per Game", "Goals/G", "Goals/Game", "GPG", "Goals per game", "GF/G",
                       "Goals For Per Game", "Scoring Offense", "Goals For/G"],
    "shots_per_game": ["Shots Per Game", "Shots/G", "Shots/Game", "SOG/G", "SOG Per Game", "Shots per game"],
    "shot_pct": ["Shot %", "Shot%", "Shot Pct", "Shot Percentage", "Shooting %", "Shooting Pct",
                 "Shooting Percentage", "SH%"],
    "pp_pct": ["Power Play %", "Power Play Pct", "Power Play Percentage", "PP%", "PP Pct",
               "Power Play Conversion %", "Power Play Conversion Percentage", "Power Play Conversion Pct"],
    "opp_pp_pct": ["Opponent Power Play %", "Opp Power Play %", "Opponent PP%", "Opp PP%", "Opp. PP%",
                   "Opponents Power Play %", "Opponent Power Play Percentage", "Opponent Power Play Pct",
                   "Opponent Power Play Conversion %", "Opponent Power Play Conversion Percentage"],
    "pk_listed": ["Penalty Kill %", "Penalty Kill Pct", "PK%", "Penalty Kill Percentage", "PK Pct",
                  "Penalty Killing %", "Penalty Killing Pct"],
    "pen_per_game": ["Penalties Per Game", "Penalties/G", "Penalties/Game", "PEN/G", "PN/G",
                     "Penalties per game", "Pen Per Game"],
    "pim_per_game": ["Penalty Minutes Per Game", "PIM/G", "PIM/Game", "Penalty Minutes/G", "Penalty Minutes/Game",
                     "PIM Per Game", "Penalty Min Per Game", "Penalty Min/G"],
    "faceoff_pct": ["Faceoff %", "Faceoff Pct", "Faceoff Percentage", "Face-off %", "FO%", "Faceoffs %",
                    "Faceoff Win %", "Faceoff Win Pct", "Face Off %", "FO Pct"],
}
TEAM_LABEL_LOOKUP = {_label_key(a): k for k, lst in TEAM_LABEL_ALIASES.items() for a in lst}


def scan_team_tables(soup: BeautifulSoup) -> Dict[str, str]:
    found: Dict[str, str] = {}
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        overall_idx: Optional[int] = None
        opp_idx: Optional[int] = None
        for r in rows[:4]:
            for i, c in enumerate(r.find_all(["th", "td"])):
                t = _label_key(c.get_text(" ", strip=True))
                if t in ("overall", "team", "all", "season") and overall_idx is None:
                    overall_idx = i
                elif t.startswith("opp") and opp_idx is None:
                    opp_idx = i

        for r in rows:
            cells = r.find_all(["th", "td"])
            if len(cells) < 2:
                continue
            texts = [c.get_text(" ", strip=True) for c in cells]
            label_i = next((i for i, t in enumerate(texts[:2]) if t and _num_text(t) is None), None)
            if label_i is None:
                continue
            key = TEAM_LABEL_LOOKUP.get(_label_key(texts[label_i]))
            if not key:
                continue
            val: Optional[str] = None
            if overall_idx is not None and overall_idx != label_i and overall_idx < len(texts):
                val = _num_text(texts[overall_idx])
            if val is None:
                for t in texts[label_i + 1:]:
                    v = _num_text(t)
                    if v is not None:
                        val = v
                        break
            if val is None:
                continue
            found.setdefault(key, val)
            if key == "pp_pct" and opp_idx is not None and opp_idx < len(texts):
                ov = _num_text(texts[opp_idx])
                if ov is not None:
                    found.setdefault("opp_pp_pct", ov)
    return found


def scan_team_text(soup: BeautifulSoup, found: Dict[str, str]) -> None:
    """Last resort for non-table layouts: 'Label' followed by a number within the next few lines."""
    lines = [ln.strip() for ln in soup.get_text("\n", strip=True).split("\n") if ln.strip()]
    for i, line in enumerate(lines):
        key = TEAM_LABEL_LOOKUP.get(_label_key(line))
        if not key or key in found:
            continue
        for nxt in lines[i + 1:i + 4]:
            v = _num_text(nxt)
            if v is not None:
                found[key] = v
                break


def derive_from_totals(found: Dict[str, str], totals: Dict[str, str], max_gp: int) -> None:
    gp = _f(totals.get("gp"))
    if gp is None or gp > 60 or gp <= 0:
        gp = float(max_gp) if max_gp else None
    g, sh = _f(totals.get("g")), _f(totals.get("sh"))

    if gp:
        if g is not None and "goals_per_game" not in found:
            found["goals_per_game"] = f"{g / gp:.4f}"
        if sh is not None and "shots_per_game" not in found:
            found["shots_per_game"] = f"{sh / gp:.4f}"
    if g is not None and sh and "shot_pct" not in found:
        found["shot_pct"] = f"{g / sh * 100:.4f}%"

    pen = pim = None
    mm = re.match(r"^(\d+)\s*[-/]\s*(\d+)$", str(totals.get("pn_pim", "")).strip())
    if mm:
        pen, pim = float(mm.group(1)), float(mm.group(2))
    else:
        pen, pim = _f(totals.get("pen")), _f(totals.get("pim"))
    if gp:
        if pen is not None and "pen_per_game" not in found:
            found["pen_per_game"] = f"{pen / gp:.4f}"
        if pim is not None and "pim_per_game" not in found:
            found["pim_per_game"] = f"{pim / gp:.4f}"

    if "faceoff_pct" not in found:
        fow, fol = _f(totals.get("fow")), _f(totals.get("fol"))
        if fow is not None and fol is not None and (fow + fol) > 0:
            found["faceoff_pct"] = f"{fow / (fow + fol) * 100:.4f}%"
        elif totals.get("fo_pct"):
            found["faceoff_pct"] = totals["fo_pct"]


def pct_number(raw: Any) -> Optional[float]:
    if raw is None:
        return None
    s = str(raw).strip()
    v = _f(s)
    if v is None:
        return None
    if not s.endswith("%") and 0 < v <= 1.0:      # .214 -> 21.4
        v *= 100
    return v


def build_team_stats(found: Dict[str, str]) -> Tuple[Dict[str, str], List[str]]:
    opp_pp = pct_number(found.get("opp_pp_pct"))
    pk_listed = pct_number(found.get("pk_listed"))
    if opp_pp is None and pk_listed is not None:
        opp_pp = 100.0 - pk_listed
    if PK_PCT_IS_OPP_PP:
        pk = opp_pp
    else:
        pk = pk_listed if pk_listed is not None else (100.0 - opp_pp if opp_pp is not None else None)

    resolved: Dict[str, Optional[float]] = {
        "goals_per_game": _f(found.get("goals_per_game")),
        "shots_per_game": _f(found.get("shots_per_game")),
        "shot_pct": pct_number(found.get("shot_pct")),
        "pp_pct": pct_number(found.get("pp_pct")),
        "pk_pct": pk,
        "pen_per_game": _f(found.get("pen_per_game")),
        "pim_per_game": _f(found.get("pim_per_game")),
        "faceoff_pct": pct_number(found.get("faceoff_pct")),
    }
    stats: Dict[str, str] = {}
    missing: List[str] = []
    for k in TEAM_STAT_KEYS:
        v = resolved[k]
        if v is None:
            missing.append(k)
        if k.endswith("_pct"):
            stats[k] = f"{(v if v is not None else 0.0):.1f}%"
        else:
            stats[k] = f"{(v if v is not None else 0.0):.2f}"
    return stats, missing


# ── Output helpers ────────────────────────────────────────────────────────────
def write_team_json(slug: str, team_data: Dict[str, Any]) -> None:
    for dest in [f"{slug}.json", os.path.join("docs", f"{slug}.json")]:
        with open(dest, "w", encoding="utf-8") as f:
            json.dump(team_data, f, indent=2)


def read_existing_json(slug: str) -> Dict[str, Any]:
    try:
        with open(os.path.join("docs", f"{slug}.json"), encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _dump(debug_dir: str, filename: str, html: str) -> None:
    try:
        os.makedirs(debug_dir, exist_ok=True)
        with open(os.path.join(debug_dir, filename), "w", encoding="utf-8") as f:
            f.write(html)
    except Exception as e:
        logger.warning(f"Could not write debug file {filename}: {e}")


def finalize_player(p: Dict[str, Any]) -> Dict[str, str]:
    pn_pim = p.get("pn_pim", "")
    if not pn_pim and (p.get("pen") or p.get("pim")):
        pn_pim = f"{clean_stat_int(p.get('pen'))}-{clean_stat_int(p.get('pim'))}"
    out: Dict[str, str] = {
        "number": p.get("number", ""),
        "name": p.get("name", ""),
        "position": p.get("position", ""),
        "year": p.get("year", ""),
    }
    for f in INT_FIELDS:
        out[f] = clean_stat_int(p.get(f))
    out["pn_pim"] = clean_pn_pim(pn_pim)
    return out


# ── Per-team scrape ───────────────────────────────────────────────────────────
def extract_players(html: str, style_hint: str) -> Tuple[List[Dict[str, Any]], Dict[str, str], str]:
    soup = BeautifulSoup(html, "html.parser")
    order = {"new": ["nuxt", "table"], "old": ["table", "nuxt"], "asu": ["asu", "table", "nuxt"]}.get(
        style_hint, ["nuxt", "table", "asu"])
    labels = {"nuxt": "New Style (Nuxt data)", "table": "Old Style (HTML tables)", "asu": "Arizona State (custom)"}

    best: Tuple[List[Dict[str, Any]], Dict[str, str], str] = ([], {}, "none")
    for strat in order:
        if strat == "nuxt":
            pl, tot = parse_nuxt_data_stats(html)
        else:
            pl, tot = parse_stats_tables(soup, lenient=(strat == "asu"))
        if len(pl) > len(best[0]):
            best = (pl, tot, labels[strat])
        if len(pl) >= MIN_PLAYERS_ACCEPT:
            break

    players, totals, label = best
    if not totals:
        _, totals = parse_stats_tables(soup, lenient=True)
    return players, totals, label


async def scrape_single_team(context: BrowserContext, team_cfg: Dict[str, Any],
                             semaphore: asyncio.Semaphore, debug_dir: Optional[str]) -> Dict[str, Any]:
    async with semaphore:
        slug, label = team_cfg["slug"], team_cfg["name"]
        logger.info(f"Processing {label} (Men's Hockey)...")

        candidates = [team_cfg["roster_url"]] + [c for c in derive_roster_candidates(team_cfg["stats_url"])
                                                  if c != team_cfg["roster_url"]]
        stats_html, (roster_lookup, roster_used) = await asyncio.gather(
            fetch_page_html(context, team_cfg["stats_url"], wait_for_table=True),
            fetch_roster(context, candidates, label, debug_dir, slug),
        )
        if debug_dir and stats_html:
            _dump(debug_dir, f"{slug}_stats.html", stats_html)
        logger.info(f"  [{label}] Loaded {len(roster_lookup)} roster entries")

        stats_players: List[Dict[str, Any]] = []
        totals: Dict[str, str] = {}
        style = "none"
        if stats_html:
            stats_players, totals, style = extract_players(stats_html, team_cfg["style"])
        logger.info(f"  [{label}] Parsed {len(stats_players)} player stat lines - detected: {style}")

        # Team stats: scraped labels first, then derived from the Total row
        found_team: Dict[str, str] = {}
        if stats_html:
            soup = BeautifulSoup(stats_html, "html.parser")
            found_team = scan_team_tables(soup)
            scan_team_text(soup, found_team)
            max_gp = max((int(_f(p.get("gp")) or 0) for p in stats_players), default=0)
            derive_from_totals(found_team, totals, max_gp)
        team_stats, missing = build_team_stats(found_team)
        if len(missing) == len(TEAM_STAT_KEYS):
            ts_status = "not_found"
        elif missing:
            ts_status = "partial"
        else:
            ts_status = "ok"
        logger.info(
            f"  [{label}] Team stats: {len(TEAM_STAT_KEYS) - len(missing)}/{len(TEAM_STAT_KEYS)} found"
            + (f" - missing: {', '.join(missing)}" if missing else "")
        )

        # Merge roster info (position / year / number) into the stat lines
        merged: List[Dict[str, str]] = []
        seen: set = set()
        no_pos = 0
        for sp in stats_players:
            name = sp.get("name", "")
            if not name or name.lower() in seen or not is_valid_player_name(name):
                continue
            seen.add(name.lower())

            r_info = find_roster_entry(name, sp.get("number", ""), roster_lookup)
            p = dict(sp)
            if r_info:
                p["name"] = r_info.get("name") or name
                p["number"] = sp.get("number") or r_info.get("number", "")
                p["position"] = r_info.get("position") or sp.get("position", "")
                p["year"] = r_info.get("year") or sp.get("year", "")
            if not p.get("position") and sp.get("goalie"):
                p["position"] = "G"
            if not p.get("position"):
                no_pos += 1
            merged.append(finalize_player(p))
        if no_pos:
            logger.warning(f"  [{label}] {no_pos} player(s) have no position (not found on roster)")

        existing = read_existing_json(slug)
        if ts_status == "not_found" and existing.get("team_stats") \
                and existing.get("team_stats_status") in ("ok", "partial", "kept_previous"):
            team_stats = existing["team_stats"]
            missing = existing.get("team_stats_missing", [])
            ts_status = "kept_previous"
            logger.warning(f"  [{label}] Team stats not found - keeping previous team_stats")

        team_data: Dict[str, Any] = {
            "team": label,
            "slug": slug,
            "gender": "men",
            "sport": "hockey",
            "conference": "NCHC",
            "generated_at": now_iso(),
            "source_stats_url": team_cfg["stats_url"],
            "source_roster_url": roster_used,
            "site_style": style,
            "team_stats_status": ts_status,
            "team_stats_missing": missing,
            "team_stats": team_stats,
            "players": merged,
        }

        if not merged:
            if existing.get("players"):
                logger.warning(f"  [{label}] No players parsed - keeping existing {slug}.json")
                return existing
            logger.warning(f"  [{label}] No players parsed for {slug}.json")

        write_team_json(slug, team_data)
        return team_data


async def run_scraper(only: Optional[List[str]], debug: bool) -> None:
    os.makedirs("docs", exist_ok=True)
    debug_dir = "debug" if debug else None
    semaphore = asyncio.Semaphore(CONCURRENCY_LIMIT)

    teams = TEAMS_CONFIG
    if only:
        wanted = [o.strip().lower() for o in only if o.strip()]
        teams = [t for t in TEAMS_CONFIG if any(w in t["name"].lower() or w in t["slug"] for w in wanted)]
        if not teams:
            logger.error(f"--only matched no teams: {only}")
            return

    start = time.time()
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
        context = await browser.new_context(
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
            viewport={"width": 1280, "height": 800},
            locale="en-US",
        )
        logger.info("=== Starting Concurrent NCHC Men's Hockey Scraping ===")
        results = await asyncio.gather(
            *[scrape_single_team(context, t, semaphore, debug_dir) for t in teams],
            return_exceptions=True,
        )
        await browser.close()

    failures = [r for r in results if isinstance(r, Exception)]
    for err in failures:
        logger.error(f"A team scrape failed: {err!r}")
    for t, r in zip(teams, results):
        if isinstance(r, dict):
            logger.info(f"  {t['name']:<18} players={len(r.get('players', [])):<3} "
                        f"team_stats={r.get('team_stats_status')} style={r.get('site_style', '-')}")
    logger.info(f"Done — {len(results) - len(failures)} of {len(results)} team scrapes completed "
                f"in {time.time() - start:.1f} seconds!")


def main() -> None:
    ap = argparse.ArgumentParser(description="NCHC Men's Hockey stats scraper")
    ap.add_argument("--only", help='Comma-separated team names/slugs, e.g. "St. Thomas,Denver"')
    ap.add_argument("--debug", action="store_true", help="Save raw HTML of every fetched page to ./debug/")
    args = ap.parse_args()
    asyncio.run(run_scraper(args.only.split(",") if args.only else None, args.debug))


if __name__ == "__main__":
    main()
