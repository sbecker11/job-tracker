#!/usr/bin/env python3
"""
ats_jd_resolver.py
==================

Given a COMPANY NAME and a JOB TITLE, attempt to return the full job
description (JD) text by querying the PUBLIC, no-auth job-board endpoints of
the major applicant-tracking systems (ATS):

    - Greenhouse      boards-api.greenhouse.io
    - Lever           api.lever.co
    - Ashby           api.ashbyhq.com
    - SmartRecruiters api.smartrecruiters.com

These endpoints are the same ones that power each company's public careers
page, so reading them is legitimate and does not touch LinkedIn or violate any
terms. This deliberately avoids LinkedIn scraping.

WHAT IT DOES
    1. Turns a human company name ("Included Health") into candidate board
       tokens ("includedhealth", "included-health", ...).
    2. For each ATS, finds the company's board (first token that responds with
       a real job list wins for that provider).
    3. Collects every posting's title across providers, fuzzy-matches them
       against the requested title.
    4. Fetches the FULL description for the best match and returns it as plain
       text, along with the canonical apply URL and a match score.

LIMITATIONS (read these)
    - Board-token guessing is the weak link. When a company's token isn't
      guessable, pin it in KNOWN_BOARDS below. That's the highest-value place
      to curate your target employers.
    - Workday is NOT covered: it has no clean public board API (per-tenant,
      POST-based). Most healthcare/finance/gov employers on Workday will miss.
    - Field shapes vary slightly per provider and occasionally change; parsing
      here is defensive but verify against a couple of live boards.
    - This is a prototype. Add caching/retry/rate-limiting before unattended use.

USAGE
    python ats_jd_resolver.py --company "Ancestry" --title "Senior Software Engineer"
    python ats_jd_resolver.py --company "Vercel" --title "Software Engineer" --json
    python ats_jd_resolver.py --selftest      # offline logic checks, no network

DEPENDENCIES
    pip install requests beautifulsoup4   # bs4 optional; falls back to stdlib
"""

from __future__ import annotations

import argparse
import difflib
import html
import json
import re
import sys
from dataclasses import dataclass, asdict, field
from typing import Iterable, Optional

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None  # network calls will raise a clear error; selftest still runs

try:
    from bs4 import BeautifulSoup
    _HAS_BS4 = True
except ImportError:
    _HAS_BS4 = False


# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------

REQUEST_TIMEOUT = 12  # seconds
USER_AGENT = "ats-jd-resolver/0.1 (personal job-search tool; contact: shawn.becker@spexture.com)"
DEFAULT_MATCH_THRESHOLD = 0.55  # accept best title match at/above this score

# Pin known-good board tokens here when guessing fails. Keys are lowercase
# company names; values map an ATS provider to its exact board token.
# Example:
#   "included health": {"greenhouse": "includedhealth"},
#   "ancestry":        {"greenhouse": "ancestry"},
KNOWN_BOARDS: dict[str, dict[str, str]] = {
    "stripe": {"greenhouse": "stripe"},
    "included health": {"lever": "includedhealth"},
    # Ancestry: no public Greenhouse/Lever/Ashby/SmartRecruiters board (likely Workday).
}

# Common corporate suffixes/noise words to strip when generating tokens.
_SUFFIX_NOISE = {
    "inc", "llc", "ltd", "corp", "corporation", "co", "company",
    "technologies", "technology", "software", "labs", "group", "the",
    "solutions", "systems", "global", "international",
}


# ----------------------------------------------------------------------------
# Data model
# ----------------------------------------------------------------------------

@dataclass
class Posting:
    provider: str
    board_token: str
    job_id: str
    title: str
    location: str = ""
    url: str = ""
    description: str = ""          # plain text, filled in for the winner
    match_score: float = 0.0
    _raw_description_html: str = field(default="", repr=False)  # cached if list already had it


# ----------------------------------------------------------------------------
# HTML -> text
# ----------------------------------------------------------------------------

_BLOCK_TAG_RE = re.compile(
    r"</?(?:p|br|li|div|tr|h[1-6]|ul|ol|table|section|article|header|footer)[^>]*>",
    re.IGNORECASE,
)


def html_to_text(raw: str) -> str:
    """Convert (possibly entity-escaped) HTML into clean plain text.

    Only block-level tags introduce line breaks; inline tags (<b>, <i>, <a>,
    <span>) are stripped without splitting the surrounding sentence.
    """
    if not raw:
        return ""
    # Greenhouse returns content as an HTML-entity-escaped string; unescape first.
    unescaped = html.unescape(raw)
    # Turn block-level tags into newlines BEFORE stripping the rest.
    blocked = _BLOCK_TAG_RE.sub("\n", unescaped)
    if _HAS_BS4:
        text = BeautifulSoup(blocked, "html.parser").get_text("")
    else:
        text = re.sub(r"<[^>]+>", "", blocked)
    # Tidy whitespace: collapse runs of blank lines, trim trailing spaces.
    lines = [ln.strip() for ln in text.splitlines()]
    out: list[str] = []
    blank = False
    for ln in lines:
        if ln:
            out.append(ln)
            blank = False
        elif not blank:
            out.append("")
            blank = True
    return "\n".join(out).strip()


# ----------------------------------------------------------------------------
# Token generation & title matching
# ----------------------------------------------------------------------------

def candidate_tokens(company: str) -> list[str]:
    """Generate plausible board tokens for a human company name, ordered."""
    base = company.strip().lower()
    base = re.sub(r"[^a-z0-9 ]", " ", base)
    words = [w for w in base.split() if w]
    core = [w for w in words if w not in _SUFFIX_NOISE] or words

    joined_all = "".join(words)
    joined_core = "".join(core)
    hyphen_all = "-".join(words)
    hyphen_core = "-".join(core)

    candidates = [
        joined_core,    # includedhealth
        joined_all,     # includedhealthinc
        hyphen_core,    # included-health
        hyphen_all,     # included-health-inc
        "".join(core[:1]) if core else "",  # first word only, e.g. "stripe"
    ]
    # de-dupe, preserve order, drop empties
    seen: set[str] = set()
    out: list[str] = []
    for c in candidates:
        if c and c not in seen:
            seen.add(c)
            out.append(c)
    return out


def normalize_title(t: str) -> str:
    t = t.lower()
    t = re.sub(r"[^a-z0-9 ]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    # Common ATS compound variants (fullstack ↔ full stack).
    t = t.replace("fullstack", "full stack")
    return t


def title_score(a: str, b: str) -> float:
    """0..1 similarity blending sequence ratio and token overlap."""
    na, nb = normalize_title(a), normalize_title(b)
    if not na or not nb:
        return 0.0
    ratio = difflib.SequenceMatcher(None, na, nb).ratio()
    ta, tb = set(na.split()), set(nb.split())
    union = ta | tb
    jacc = len(ta & tb) / len(union) if union else 0.0
    score = 0.5 * ratio + 0.5 * jacc
    # Boost when one title fully contains the other (e.g. exact role + extra qualifier).
    if na in nb or nb in na:
        score = min(1.0, score + 0.15)
    return round(score, 4)


# ----------------------------------------------------------------------------
# HTTP helper
# ----------------------------------------------------------------------------

def _get_json(url: str) -> Optional[object]:
    if requests is None:
        raise RuntimeError("The 'requests' package is required for network calls. pip install requests")
    try:
        resp = requests.get(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
                            timeout=REQUEST_TIMEOUT)
    except requests.RequestException:
        return None
    if resp.status_code != 200:
        return None
    try:
        return resp.json()
    except ValueError:
        return None


# ----------------------------------------------------------------------------
# Provider listers  -> return list[Posting] (descriptions filled when free)
# ----------------------------------------------------------------------------

def list_greenhouse(token: str) -> list[Posting]:
    data = _get_json(f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs")
    if not isinstance(data, dict) or "jobs" not in data:
        return []
    out = []
    for j in data["jobs"]:
        out.append(Posting(
            provider="greenhouse", board_token=token, job_id=str(j.get("id", "")),
            title=j.get("title", ""), location=(j.get("location") or {}).get("name", ""),
            url=j.get("absolute_url", ""),
        ))
    return out


def list_lever(token: str) -> list[Posting]:
    data = _get_json(f"https://api.lever.co/v0/postings/{token}?mode=json")
    if not isinstance(data, list):
        return []
    out = []
    for p in data:
        cat = p.get("categories") or {}
        # Lever already returns the full description in the list response.
        desc_html = p.get("description", "")
        for block in p.get("lists", []) or []:
            desc_html += f"<h4>{block.get('text','')}</h4><ul>{block.get('content','')}</ul>"
        desc_html += p.get("additional", "")
        out.append(Posting(
            provider="lever", board_token=token, job_id=str(p.get("id", "")),
            title=p.get("text", ""), location=cat.get("location", ""),
            url=p.get("hostedUrl", "") or p.get("applyUrl", ""),
            _raw_description_html=desc_html,
        ))
    return out


def list_ashby(token: str) -> list[Posting]:
    data = _get_json(f"https://api.ashbyhq.com/posting-api/job-board/{token}?includeCompensation=true")
    if not isinstance(data, dict):
        return []
    jobs = data.get("jobs") or data.get("data") or []
    out = []
    for j in jobs:
        out.append(Posting(
            provider="ashby", board_token=token, job_id=str(j.get("id", "")),
            title=j.get("title", ""),
            location=j.get("location", "") or (j.get("address") or {}).get("postalAddress", ""),
            url=j.get("jobUrl", "") or j.get("applyUrl", ""),
            _raw_description_html=j.get("descriptionHtml", "") or j.get("descriptionPlain", "") or "",
        ))
    return out


def list_smartrecruiters(token: str) -> list[Posting]:
    data = _get_json(f"https://api.smartrecruiters.com/v1/companies/{token}/postings")
    if not isinstance(data, dict) or "content" not in data:
        return []
    out = []
    for p in data["content"]:
        loc = p.get("location") or {}
        loc_str = ", ".join(x for x in [loc.get("city", ""), loc.get("country", "")] if x)
        out.append(Posting(
            provider="smartrecruiters", board_token=token, job_id=str(p.get("id", "")),
            title=p.get("name", ""), location=loc_str,
            url=(p.get("ref", "") or ""),
        ))
    return out


PROVIDERS = {
    "greenhouse": list_greenhouse,
    "lever": list_lever,
    "ashby": list_ashby,
    "smartrecruiters": list_smartrecruiters,
}


# ----------------------------------------------------------------------------
# Full-JD fetchers for the winning posting
# ----------------------------------------------------------------------------

def fetch_full_description(p: Posting) -> str:
    # If the lister already grabbed the HTML (Lever, Ashby), just convert it.
    if p._raw_description_html:
        return html_to_text(p._raw_description_html)

    if p.provider == "greenhouse":
        data = _get_json(f"https://boards-api.greenhouse.io/v1/boards/{p.board_token}/jobs/{p.job_id}")
        if isinstance(data, dict):
            return html_to_text(data.get("content", ""))

    if p.provider == "smartrecruiters":
        data = _get_json(
            f"https://api.smartrecruiters.com/v1/companies/{p.board_token}/postings/{p.job_id}")
        if isinstance(data, dict):
            sections = ((data.get("jobAd") or {}).get("sections") or {})
            order = ["companyDescription", "jobDescription", "qualifications", "additionalInformation"]
            parts = []
            for key in order:
                sec = sections.get(key) or {}
                title = sec.get("title", "")
                text = html_to_text(sec.get("text", ""))
                if text:
                    parts.append(f"{title}\n{text}" if title else text)
            return "\n\n".join(parts)

    return ""


# ----------------------------------------------------------------------------
# Top-level resolver
# ----------------------------------------------------------------------------

def _board_tokens_for(company: str, provider: str) -> list[str]:
    pinned = KNOWN_BOARDS.get(company.strip().lower(), {})
    tokens = []
    if provider in pinned:
        tokens.append(pinned[provider])
    tokens.extend(candidate_tokens(company))
    # de-dupe preserve order
    seen, out = set(), []
    for t in tokens:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def gather_postings(company: str, providers: Iterable[str] = PROVIDERS.keys(),
                    verbose: bool = False) -> list[Posting]:
    """Find each provider's board for the company and collect all postings."""
    collected: list[Posting] = []
    for provider in providers:
        lister = PROVIDERS[provider]
        for token in _board_tokens_for(company, provider):
            postings = lister(token)
            if postings:
                if verbose:
                    print(f"[{provider}] board '{token}' -> {len(postings)} postings", file=sys.stderr)
                collected.extend(postings)
                break  # first working token wins for this provider
            elif verbose:
                print(f"[{provider}] board '{token}' -> none", file=sys.stderr)
    return collected


def resolve(company: str, title: str, threshold: float = DEFAULT_MATCH_THRESHOLD,
            top_n: int = 5, verbose: bool = False) -> dict:
    """
    Returns a dict:
      {
        "company", "requested_title",
        "match": Posting-as-dict with full description (or None),
        "accepted": bool,
        "candidates": [top-N Posting dicts with scores, no descriptions],
      }
    """
    postings = gather_postings(company, verbose=verbose)
    for p in postings:
        p.match_score = title_score(title, p.title)
    postings.sort(key=lambda x: x.match_score, reverse=True)

    candidates = postings[:top_n]
    result = {
        "company": company,
        "requested_title": title,
        "match": None,
        "accepted": False,
        "candidates": [
            {"provider": c.provider, "title": c.title, "location": c.location,
             "url": c.url, "match_score": c.match_score}
            for c in candidates
        ],
    }

    if postings and postings[0].match_score >= threshold:
        best = postings[0]
        best.description = fetch_full_description(best)
        result["match"] = asdict(best)
        result["match"].pop("_raw_description_html", None)
        result["accepted"] = True

    return result


# ----------------------------------------------------------------------------
# Offline self-test (no network)
# ----------------------------------------------------------------------------

def _selftest() -> int:
    ok = True

    def check(name, cond):
        nonlocal ok
        print(f"  {'PASS' if cond else 'FAIL'}  {name}")
        ok = ok and cond

    print("Token generation:")
    toks = candidate_tokens("Included Health, Inc.")
    check("strips suffix -> includedhealth", "includedhealth" in toks)
    check("hyphen variant -> included-health", "included-health" in toks)
    toks2 = candidate_tokens("Ancestry")
    check("single word -> ancestry", toks2[0] == "ancestry")

    print("HTML to text:")
    txt = html_to_text("&lt;p&gt;Build &lt;b&gt;data&lt;/b&gt; pipelines&lt;/p&gt;&lt;li&gt;Java&lt;/li&gt;")
    check("unescapes + strips tags", "Build data pipelines" in txt and "Java" in txt)
    check("no angle brackets remain", "<" not in txt and ">" not in txt)

    print("Title matching:")
    check("exact match ~1.0", title_score("Senior Software Engineer", "Senior Software Engineer") > 0.95)
    check("contains qualifier scores high",
          title_score("Senior Software Engineer", "Senior Software Engineer, Backend") >= 0.6)
    check("unrelated scores low", title_score("Senior Software Engineer", "Marketing Manager") < 0.4)
    s_good = title_score("Data Engineer", "Senior Data Engineer")
    s_bad = title_score("Data Engineer", "Sales Director")
    check("ranks relevant above irrelevant", s_good > s_bad)

    print()
    print("SELFTEST:", "ALL PASS" if ok else "FAILURES PRESENT")
    return 0 if ok else 1


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------

def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Resolve a full job description from public ATS board APIs.")
    ap.add_argument("--company", help="Company name, e.g. 'Ancestry'")
    ap.add_argument("--title", help="Job title, e.g. 'Senior Software Engineer'")
    ap.add_argument("--threshold", type=float, default=DEFAULT_MATCH_THRESHOLD,
                    help=f"Min title match score to accept (default {DEFAULT_MATCH_THRESHOLD})")
    ap.add_argument("--json", action="store_true", help="Emit JSON instead of human-readable text")
    ap.add_argument("--verbose", action="store_true", help="Log board lookups to stderr")
    ap.add_argument("--selftest", action="store_true", help="Run offline logic checks and exit")
    args = ap.parse_args(argv)

    if args.selftest:
        return _selftest()

    if not args.company or not args.title:
        ap.error("--company and --title are required (or use --selftest)")

    result = resolve(args.company, args.title, threshold=args.threshold, verbose=args.verbose)

    if args.json:
        print(json.dumps(result, indent=2))
        return 0 if result["accepted"] else 2

    # Human-readable
    print(f"Company : {result['company']}")
    print(f"Wanted  : {result['requested_title']}")
    print("-" * 60)
    if result["accepted"]:
        m = result["match"]
        print(f"MATCH   : {m['title']}  (score {m['match_score']}, via {m['provider']})")
        print(f"Location: {m['location']}")
        print(f"URL     : {m['url']}")
        print("-" * 60)
        print(m["description"] or "(no description text returned)")
        return 0
    else:
        print("No confident match found.")
        if result["candidates"]:
            print("\nClosest titles seen (below threshold):")
            for c in result["candidates"]:
                print(f"  {c['match_score']:>5}  {c['title']}  [{c['provider']}]  {c['url']}")
        else:
            print("No board found for that company on Greenhouse/Lever/Ashby/SmartRecruiters.")
            print("Tip: pin the correct board token in KNOWN_BOARDS, or the company may use Workday (unsupported).")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
