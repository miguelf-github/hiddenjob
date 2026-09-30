#!/usr/bin/env python3
"""A private, configuration-first job discovery and review ledger.

The program finds less-visible postings from publisher-declared sitemaps and
records source evidence. Its optional ATS adapter can prefill a reviewed form;
submission, email, account creation, and complaint filing remain deliberate
human actions guarded by explicit flags.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from xml.etree import ElementTree

UA = "Hiddenjob/0.1 (research-and-review)"
# RemoteOK challenges non-browser User-Agents; their API terms also require a
# follow (non-nofollow) link-back attribution wherever their data is shown.
BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


def _secret_from_file(name: str) -> str:
    """Read a KEY=VALUE secret from ~/.config/hiddenjob.env (chmod 600).

    Local fallback so API keys never have to be exported in the shell or
    hardcoded. Returns "" when the file or key is absent.
    """
    path = os.path.expanduser("~/.config/hiddenjob.env")
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                if key.strip() == name:
                    return value.strip().strip("\"").strip("'")
    except OSError:
        pass
    return ""
PERM_MARKERS = [
    (r"\bforeign\s+equivalent\b", 3),
    (r"\bin the job offered\b", 3),
    (r"\b(?:mail|send)\s+(?:your\s+)?resumes?\b", 3),
    (r"\bnotice of filing\b|\blabor certification\b", 3),
    (r"\bminimum requirements?\b|\bjob duties\b|\bspecial (?:skill )?requirements?\b", 2),
    (r"\btelecommut|\bprogressively responsible\b|\bPERM\b", 2),
]
LCA_MARKERS = [
    (r"\blabor condition application\b", 3),
    (r"\bH-?1B\b|\bnon-?immigrant worker\b|\bSOC code\b|\bwage level\b", 2),
    (r"\bperiod of employment\b|\bpublic access file\b", 1),
]


class FetchBlocked(RuntimeError):
    """A source cannot be measured; it must not be treated as an empty result."""


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def clean_text(raw: str) -> str:
    raw = re.sub(r"<(script|style|noscript)[^>]*>.*?</\1>", " ", raw, flags=re.I | re.S)
    raw = re.sub(r"<[^>]+>", " ", raw)
    return re.sub(r"\s+", " ", html.unescape(raw)).strip()


def classify(text: str) -> dict[str, object]:
    """Classify wording, not legal status or employer intent."""
    def score(markers: list[tuple[str, int]]) -> tuple[int, list[str]]:
        hits = [pattern for pattern, _ in markers if re.search(pattern, text, re.I)]
        return sum(weight for pattern, weight in markers if pattern in hits), hits

    perm, perm_hits = score(PERM_MARKERS)
    lca, lca_hits = score(LCA_MARKERS)
    if perm >= 5:
        kind = "perm-like"
    elif lca >= 6:
        kind = "lca-like"
    elif re.search(r"equally or better qualified U\.?S\.? worker|H-?1B dependent", text, re.I):
        kind = "recruitment-notice-like"
    else:
        kind = "unclassified"
    return {"classification": kind, "perm_score": perm, "lca_score": lca,
            "marker_hits": perm_hits + lca_hits}


def is_challenge_page(lower_body: str) -> bool:
    """Identify bot-wall signatures without rejecting ordinary job-page wording."""
    return ("just a moment" in lower_body or "challenge-platform" in lower_body or
            "cf-chl-" in lower_body or ("hcaptcha" in lower_body and "challenge" in lower_body))


def fetch(url: str, timeout: int = 25) -> tuple[str, str]:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html,application/xml;q=0.9,*/*;q=0.8"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            body = response.read().decode("utf-8", "replace")
            status, final_url = response.status, response.geturl()
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return "", url
        raise FetchBlocked(f"{url}: HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise FetchBlocked(f"{url}: {exc}") from exc
    lower = body[:8000].lower()
    # Do not treat ordinary page text mentioning CAPTCHA as a block. Detect the
    # known interstitial signatures instead.
    if status != 200 or not body.strip() or is_challenge_page(lower):
        raise FetchBlocked(f"{url}: unmeasurable response (HTTP {status}, {len(body)} bytes)")
    return body, final_url


def fetch_json(url: str, timeout: int = 25, user_agent: str | None = None) -> object:
    req = urllib.request.Request(url, headers={"User-Agent": user_agent or UA, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            if response.status != 200:
                raise FetchBlocked(f"{url}: HTTP {response.status}")
            return json.loads(response.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        raise FetchBlocked(f"{url}: HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        raise FetchBlocked(f"{url}: {exc}") from exc


def hackernews_jobs(limit: int = 50, timeout: int = 25) -> list[dict[str, object]]:
    """Fetch job postings from Hacker News via the public Firebase API.

    Uses only the official HN API (no page scraping). Returns job records in
    the same shape as extract_job() so they flow through the normal
    store/classify/rank pipeline. Company is parsed from the title prefix
    (e.g. "Stable (YC W20) Is Hiring..." -> "Stable").
    """
    try:
        ids = fetch_json("https://hacker-news.firebaseio.com/v0/jobstories.json", timeout=timeout)
    except FetchBlocked as exc:
        print(f"hackernews: {exc}", file=sys.stderr)
        return []
    if not isinstance(ids, list):
        return []
    records: list[dict[str, object]] = []
    for job_id in ids[:limit]:
        try:
            item = fetch_json(f"https://hacker-news.firebaseio.com/v0/item/{job_id}.json", timeout=timeout)
        except FetchBlocked:
            continue
        if not isinstance(item, dict) or item.get("type") != "job":
            continue
        title = str(item.get("title") or "Untitled posting")
        # Company is typically the text before " is hiring" / " hiring" / " (".
        company = ""
        m = re.match(r"^(.+?)\s+(?:is\s+)?hiring\b", title, re.I)
        if m:
            company = re.sub(r"\s*\(.*?\)\s*$", "", m.group(1)).strip()
        url = str(item.get("url") or f"https://news.ycombinator.com/item?id={job_id}")
        text = str(item.get("text") or "")
        description = clean_text(f"{title}\n{text}") if text else clean_text(title)
        posted = item.get("time")
        date_posted = ""
        if isinstance(posted, (int, float)):
            try:
                date_posted = datetime.fromtimestamp(posted, UTC).isoformat()
            except (ValueError, OSError):
                pass
        records.append({
            "url": url,
            "source": "hackernews",
            "title": title[:300],
            "company": company[:300],
            "date_posted": date_posted,
            "description": description,
            "description_sha256": hashlib.sha256(description.encode()).hexdigest(),
            **classify(description),
        })
    return records


def remoteok_jobs(limit: int = 100, timeout: int = 25) -> list[dict[str, object]]:
    """Fetch remote postings from the RemoteOK public API.

    Keyless JSON feed; requires a browser-like User-Agent (bot UAs are
    challenged). Element 0 of the response is a legal/terms notice, not a
    job — records are identified by the presence of a "position" key.
    Attribution: RemoteOK's API terms require a follow (non-nofollow)
    link-back to remoteok.com wherever their data is displayed.
    """
    try:
        data = fetch_json("https://remoteok.com/api", timeout=timeout, user_agent=BROWSER_UA)
    except FetchBlocked as exc:
        print(f"remoteok: {exc}", file=sys.stderr)
        return []
    if not isinstance(data, list):
        return []
    records: list[dict[str, object]] = []
    for job in data:
        if not isinstance(job, dict) or "position" not in job:
            continue
        title = str(job.get("position") or "Untitled posting")
        description = clean_text(str(job.get("description") or ""))
        records.append({
            "url": str(job.get("url") or ""),
            "source": "remoteok",
            "title": title[:300],
            "company": str(job.get("company") or "").strip()[:300],
            "date_posted": str(job.get("date") or ""),
            "description": description,
            "description_sha256": hashlib.sha256(description.encode()).hexdigest(),
            **classify(description),
        })
        if len(records) >= limit:
            break
    return records


def remotive_jobs(limit: int = 100, timeout: int = 25) -> list[dict[str, object]]:
    """Fetch remote postings from the Remotive public API (keyless JSON)."""
    try:
        data = fetch_json(f"https://remotive.com/api/remote-jobs?limit={limit}", timeout=timeout)
    except FetchBlocked as exc:
        print(f"remotive: {exc}", file=sys.stderr)
        return []
    jobs = data.get("jobs", []) if isinstance(data, dict) else []
    records: list[dict[str, object]] = []
    for job in jobs:
        if not isinstance(job, dict):
            continue
        title = str(job.get("title") or "Untitled posting")
        description = clean_text(str(job.get("description") or ""))
        records.append({
            "url": str(job.get("url") or ""),
            "source": "remotive",
            "title": title[:300],
            "company": str(job.get("company_name") or "").strip()[:300],
            "date_posted": str(job.get("publication_date") or ""),
            "description": description,
            "description_sha256": hashlib.sha256(description.encode()).hexdigest(),
            **classify(description),
        })
    return records[:limit]


def arbeitnow_jobs(limit: int = 100, timeout: int = 25) -> list[dict[str, object]]:
    """Fetch postings from the Arbeitnow public API (keyless JSON).

    Supplemental source: heavily German/EU-leaning. Their terms ask for a
    link-back to arbeitnow.com; jobs refresh hourly ordered by created_at.
    """
    try:
        data = fetch_json("https://www.arbeitnow.com/api/job-board-api", timeout=timeout)
    except FetchBlocked as exc:
        print(f"arbeitnow: {exc}", file=sys.stderr)
        return []
    jobs = data.get("data", []) if isinstance(data, dict) else []
    records: list[dict[str, object]] = []
    for job in jobs:
        if not isinstance(job, dict):
            continue
        title = str(job.get("title") or "Untitled posting")
        description = clean_text(str(job.get("description") or ""))
        created = job.get("created_at")
        date_posted = ""
        if isinstance(created, (int, float)):
            try:
                date_posted = datetime.fromtimestamp(created, UTC).isoformat()
            except (ValueError, OSError):
                pass
        records.append({
            "url": str(job.get("url") or ""),
            "source": "arbeitnow",
            "title": title[:300],
            "company": str(job.get("company_name") or "").strip()[:300],
            "date_posted": date_posted,
            "description": description,
            "description_sha256": hashlib.sha256(description.encode()).hexdigest(),
            **classify(description),
        })
        if len(records) >= limit:
            break
    return records


USAJOBS_SITE_URL = "https://www.usajobs.gov/Search/ExecuteSearch"
# First-party browser UA: the site's bot mitigation tarpits requests that do
# not look like a browser (missing Accept/Sec-Fetch-*/Origin/Referer headers
# hang the connection). Verified 2026-09-28.
_USAJOBS_SITE_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/126.0.0.0 Safari/537.36")


def usajobs_site_jobs(limit: int = 50, timeout: int = 25, keyword: str = "") -> list[dict[str, object]]:
    """Fetch federal postings via the usajobs.gov site's own search endpoint.

    Keyless fallback for when the developer API key is rejected (HTTP 401):
    POST https://www.usajobs.gov/Search/ExecuteSearch with a JSON body —
    the same contract the site's frontend uses (reverse-engineered from
    /js/searchmain.js 2026-09-28). No API key, no cookies needed; the
    request must carry browser-like headers or the server never responds.

    Returns records in the same normalized shape as usajobs_jobs().
    """
    per_page = max(1, min(limit, 50))
    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Origin": "https://www.usajobs.gov",
        "Referer": "https://www.usajobs.gov/search/results/",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
        "User-Agent": _USAJOBS_SITE_UA,
    }
    records: list[dict[str, object]] = []
    page = 1
    while len(records) < limit:
        body = json.dumps({
            "Keyword": keyword,
            "Page": str(page),
            "ResultsPerPage": per_page,
        }).encode("utf-8")
        req = urllib.request.Request(USAJOBS_SITE_URL, data=body,
                                     headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                if response.status != 200:
                    raise FetchBlocked(f"usajobs-site: HTTP {response.status}")
                data = json.loads(response.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            print(f"usajobs-site: HTTP {exc.code}", file=sys.stderr)
            return records
        except (urllib.error.URLError, TimeoutError, OSError,
                json.JSONDecodeError) as exc:
            print(f"usajobs-site: {exc}", file=sys.stderr)
            return records
        jobs = data.get("Jobs") if isinstance(data, dict) else None
        if not jobs:
            break
        for job in jobs:
            if not isinstance(job, dict):
                continue
            uri = str(job.get("PositionURI") or "").replace(":443/", "/", 1)
            hiring = job.get("HiringPath") or []
            hiring_txt = ", ".join(str(h.get("SearchDisplay") or "")
                                   for h in hiring if isinstance(h, dict))
            description = clean_text("\n".join(
                s for s in [
                    str(job.get("Title") or ""),
                    " — ".join(s for s in [str(job.get("Agency") or ""),
                                          str(job.get("Department") or "")]
                              if s),
                    str(job.get("LocationDisplay") or ""),
                    str(job.get("SalaryDisplay") or ""),
                    f"Hiring path: {hiring_txt}" if hiring_txt else "",
                    f"Work: {job.get('WorkSchedule') or ''} "
                    f"{job.get('WorkType') or ''}".strip(),
                    f"Closes: {job.get('PositionEndDate') or ''}",
                    f"Position ID: {job.get('PositionID') or ''}",
                ] if s.strip()))
            records.append({
                "url": uri,
                "source": "usajobs",
                "title": str(job.get("Title") or "Untitled posting")[:300],
                "company": str(job.get("Agency") or "").strip()[:300],
                "date_posted": str(job.get("PositionStartDate") or ""),
                "description": description,
                "description_sha256": hashlib.sha256(
                    description.encode()).hexdigest(),
                **classify(description),
            })
            if len(records) >= limit:
                break
        pager = data.get("Pager") if isinstance(data, dict) else None
        if not isinstance(pager, dict) or not pager.get("HasNextPage"):
            break
        page += 1
        time.sleep(0.5)
    return records[:limit]


USAJOBS_HISTORIC_URL = "https://data.usajobs.gov/api/historicjoa"


def usajobs_historic_jobs(limit: int = 200, timeout: int = 30,
                          days_back: int = 3, keyword: str = "") -> list[dict[str, object]]:
    """Fetch federal postings via the keyless historic-announcement endpoint.

    GET https://data.usajobs.gov/api/historicjoa?StartPositionOpenDate=...&EndPositionOpenDate=...
    Same host as the keyed API, but this path enforces no Authorization-Key
    (verified live 2026-09-28: HTTP 200, 363 records for a 2-day window, no
    key, no special headers). Returns 40-field records — richer than the
    site-search cards (agency codes, salary numbers, telework, clearance,
    whoMayApply, structured locations).

    The window covers announcements that OPENED in the last `days_back`
    days, so it works as a fresh-jobs source for daily discovery. Record
    URLs use the same https://www.usajobs.gov/job/{controlNumber} identity
    as usajobs_site_jobs(), so cross-source dedup by URL holds.
    """
    end = date.today()
    start = end - timedelta(days=max(1, days_back))
    query = urllib.parse.urlencode({
        "StartPositionOpenDate": start.isoformat(),
        "EndPositionOpenDate": end.isoformat(),
    })
    req = urllib.request.Request(
        f"{USAJOBS_HISTORIC_URL}?{query}",
        headers={"Accept": "application/json", "User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            if response.status != 200:
                raise FetchBlocked(f"usajobs-historic: HTTP {response.status}")
            raw = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        print(f"usajobs-historic: HTTP {exc.code}", file=sys.stderr)
        return []
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        print(f"usajobs-historic: {exc}", file=sys.stderr)
        return []
    if not raw.strip() or raw.strip() == "204 No Content":
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"usajobs-historic: bad JSON {exc}", file=sys.stderr)
        return []
    items = data.get("data") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return []
    kw = keyword.strip().lower()
    records: list[dict[str, object]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        control = str(item.get("usajobsControlNumber") or "")
        if not control:
            continue
        title = str(item.get("positionTitle") or "Untitled posting")
        agency = str(item.get("hiringAgencyName") or "")
        dept = str(item.get("hiringDepartmentName") or "")
        if kw and kw not in f"{title} {agency} {dept}".lower():
            continue
        locs = item.get("positionlocations") or []
        loc_txt = ", ".join(
            ", ".join(s for s in [str(l.get("positionLocationCity") or ""),
                                  str(l.get("positionLocationState") or "")]
                      if s)
            for l in locs if isinstance(l, dict)) if isinstance(locs, list) else ""
        sal_min = item.get("minimumSalary")
        sal_max = item.get("maximumSalary")
        salary = ""
        if sal_min or sal_max:
            salary = f"${sal_min} - ${sal_max} ({item.get('salaryType') or ''})".strip()
        description = clean_text("\n".join(
            s for s in [
                title,
                " — ".join(s for s in [agency, dept] if s),
                loc_txt,
                salary,
                f"Telework: {item.get('teleworkEligible')}" if item.get("teleworkEligible") else "",
                f"Clearance: {item.get('securityClearance')}" if item.get("securityClearance") else "",
                f"Who may apply: {item.get('whoMayApply')}" if item.get("whoMayApply") else "",
                f"Work schedule: {item.get('workSchedule') or ''}".rstrip(": "),
                f"Opens: {item.get('positionOpenDate') or ''}  Closes: {item.get('positionCloseDate') or ''}",
                f"Announcement: {item.get('announcementNumber') or ''}",
            ] if s.strip()))
        records.append({
            "url": f"https://www.usajobs.gov/job/{control}",
            "source": "usajobs",
            "title": title[:300],
            "company": agency[:300],
            "date_posted": str(item.get("positionOpenDate") or ""),
            "description": description,
            "description_sha256": hashlib.sha256(
                description.encode()).hexdigest(),
            **classify(description),
        })
        if len(records) >= limit:
            break
    return records


def usajobs_jobs(limit: int = 50, timeout: int = 25, api_key: str = "",
                 keyword: str = "", user_agent_email: str = "") -> list[dict[str, object]]:
    """Fetch federal postings from the USAJOBS search API.

    Requires a free API key (https://developer.usajobs.gov/apirequest/ --
    USAJOBS also accepts api.data.gov keys). The key is never hardcoded; pass
    it in or set the env var named by the source's api_key_env. USAJOBS-issued
    keys additionally require the User-Agent header to be the registration
    email; pass it as user_agent_email, set USAJOBS_USER_AGENT, or leave ""
    for the default UA.

    Keyless route: when the USAJOBS_SHIM_URL env var is set (e.g.
    http://127.0.0.1:18711), requests go through the local key shim
    (~/workspace/tools/usajobs-shim/), which injects the key server-side --
    no key is needed in this process at all.

    Fallback: when the API path is configured (shim or key) but the request
    fails (e.g. HTTP 401 -- the key is rejected server-side), this falls back
    to usajobs_site_jobs(), the site's own keyless search endpoint, so
    federal jobs keep flowing until the API key issue is resolved.

    Shape is defensive: SearchResult.SearchResultItems[].MatchedObjectDescriptor.
    """
    params = {"ResultsPerPage": max(1, min(limit, 500)), "Fields": "full"}
    if keyword:
        params["Keyword"] = keyword
    query = urllib.parse.urlencode(params)
    shim_url = os.environ.get("USAJOBS_SHIM_URL", "").rstrip("/")
    if shim_url:
        # Keyless: the shim injects Host/User-Agent/Authorization-Key itself.
        url = f"{shim_url}/api/search?{query}"
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
    else:
        if not api_key:
            return []
        user_agent = user_agent_email or os.environ.get("USAJOBS_USER_AGENT") or UA
        url = "https://data.usajobs.gov/api/search?" + query
        req = urllib.request.Request(url, headers={
            "Host": "data.usajobs.gov",
            "User-Agent": user_agent,
            "Authorization-Key": api_key,
            "Accept": "application/json",
        })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            if response.status != 200:
                raise FetchBlocked(f"usajobs: HTTP {response.status}")
            data = json.loads(response.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        print(f"usajobs: HTTP {exc.code} — falling back to site search",
              file=sys.stderr)
        return usajobs_site_jobs(limit=limit, timeout=timeout, keyword=keyword)
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        print(f"usajobs: {exc} — falling back to site search", file=sys.stderr)
        return usajobs_site_jobs(limit=limit, timeout=timeout, keyword=keyword)
    items = []
    if isinstance(data, dict):
        sr = data.get("SearchResult") or {}
        items = sr.get("SearchResultItems", []) if isinstance(sr, dict) else []
    records: list[dict[str, object]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        d = item.get("MatchedObjectDescriptor")
        if not isinstance(d, dict):
            continue
        title = str(d.get("PositionTitle") or "Untitled posting")
        summary = d.get("JobSummary") or ""
        if not summary and isinstance(d.get("UserArea"), dict):
            summary = ((d["UserArea"].get("Details") or {}).get("JobSummary") or "")
        description = clean_text(str(summary))
        org = d.get("OrganizationName") or ""
        records.append({
            "url": str(d.get("PositionURI") or ""),
            "source": "usajobs",
            "title": title[:300],
            "company": str(org).strip()[:300],
            "date_posted": str(d.get("PublicationStartDate") or ""),
            "description": description,
            "description_sha256": hashlib.sha256(description.encode()).hexdigest(),
            **classify(description),
        })
    return records[:limit]


def adzuna_jobs(limit: int = 50, timeout: int = 25, app_id: str = "",
                app_key: str = "", keyword: str = "", location: str = "") -> list[dict[str, object]]:
    """Fetch US postings from the Adzuna search API (broad aggregator coverage).

    Free app_id/app_key from https://developer.adzuna.com/signup
    (no card; 25/min, 250/day, 2500/month). results_per_page maxes at 50.
    Salary caveat: Adzuna predicts a range when the posting states none
    (salary_is_predicted); predicted ranges are labeled as estimates and never
    presented as employer-stated pay.
    """
    if not app_id or not app_key:
        return []
    params = {
        "app_id": app_id,
        "app_key": app_key,
        "results_per_page": str(max(1, min(limit, 50))),
        "content-type": "application/json",
    }
    if keyword:
        params["what"] = keyword
    if location:
        params["where"] = location
    url = "https://api.adzuna.com/v1/api/jobs/us/search/1?" + urllib.parse.urlencode(params)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as response:
            if response.status != 200:
                raise FetchBlocked(f"adzuna: HTTP {response.status}")
            data = json.loads(response.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        print(f"adzuna: HTTP {exc.code}", file=sys.stderr)
        return []
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        print(f"adzuna: {exc}", file=sys.stderr)
        return []
    results = data.get("results", []) if isinstance(data, dict) else []
    records: list[dict[str, object]] = []
    for item in results:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "Untitled posting")
        company = str((item.get("company") or {}).get("display_name") or "")
        loc = str((item.get("location") or {}).get("display_name") or "")
        desc = clean_text(str(item.get("description") or ""))
        contract = str(item.get("contract_time") or "").replace("_", " ")
        smin, smax = item.get("salary_min"), item.get("salary_max")
        predicted = str(item.get("salary_is_predicted") or "") == "1"
        pay_note = ""
        if isinstance(smin, (int, float)) and isinstance(smax, (int, float)) and (smin or smax):
            tag = "Adzuna estimate" if predicted else "stated"
            # Adzuna sometimes reports hourly figures; tiny values are hourly, not yearly.
            per = "/hr" if smax < 500 else "/yr"
            pay_note = f" Pay: ${smin:,.0f}-${smax:,.0f}{per} ({tag})."
        parts = [p for p in [f"{title} at {company}".strip(), loc, contract] if p]
        description = clean_text(". ".join(parts) + "." + pay_note + " " + desc)
        records.append({
            "url": str(item.get("redirect_url") or ""),
            "source": "adzuna",
            "title": title[:300],
            "company": company[:300],
            "date_posted": str(item.get("created") or ""),
            "description": description,
            "description_sha256": hashlib.sha256(description.encode()).hexdigest(),
            **classify(description),
        })
    return records[:limit]


def jsearch_jobs(limit: int = 30, timeout: int = 25, api_key: str = "",
                 keyword: str = "", location: str = "") -> list[dict[str, object]]:
    """Fetch US postings via JSearch (RapidAPI), aggregating Google for Jobs
    results (Indeed, LinkedIn, Glassdoor, ZipRecruiter, company sites).

    Key from https://rapidapi.com/letscrape-6bRBa3QguO5/api/jsearch (free tier
    ~200 req/month). Key travels in the X-RapidAPI-Key header. JSearch serves
    ~10 results/page, so pages are capped at 3 per sync to protect the quota.
    Full descriptions come back inline; salary periods (HOUR/YEAR/...) are
    preserved for pay-floor filtering.
    """
    if not api_key:
        return []
    query = keyword.strip()
    if location:
        query = f"{query} in {location}".strip()
    pages = max(1, min(3, (limit + 9) // 10))
    records: list[dict[str, object]] = []
    for page in range(1, pages + 1):
        params = {"query": query or "jobs", "page": str(page), "country": "us",
                  "date_posted": "month"}
        url = "https://jsearch.p.rapidapi.com/search?" + urllib.parse.urlencode(params)
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": UA,
                "X-RapidAPI-Key": api_key,
                "X-RapidAPI-Host": "jsearch.p.rapidapi.com",
                "Accept": "application/json",
            })
            with urllib.request.urlopen(req, timeout=timeout) as response:
                if response.status != 200:
                    raise FetchBlocked(f"jsearch: HTTP {response.status}")
                data = json.loads(response.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            print(f"jsearch: HTTP {exc.code}", file=sys.stderr)
            return records
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            print(f"jsearch: {exc}", file=sys.stderr)
            return records
        items = data.get("data", []) if isinstance(data, dict) else []
        for item in items:
            if not isinstance(item, dict):
                continue
            title = str(item.get("job_title") or "Untitled posting")
            company = str(item.get("employer_name") or "")
            city = str(item.get("job_city") or "")
            state = str(item.get("job_state") or "")
            loc = ", ".join(p for p in [city, state] if p) or str(item.get("job_country") or "")
            desc = clean_text(str(item.get("job_description") or ""))
            etype = str(item.get("job_employment_type") or "").replace("_", " ")
            smin, smax, period = item.get("job_min_salary"), item.get("job_max_salary"), str(item.get("job_salary_period") or "")
            pay_note = ""
            if isinstance(smin, (int, float)) and isinstance(smax, (int, float)) and (smin or smax):
                per = {"YEAR": "/yr", "MONTH": "/mo", "WEEK": "/wk", "DAY": "/day", "HOUR": "/hr"}.get(period, "")
                pay_note = f" Pay: ${smin:,.0f}-${smax:,.0f}{per} (stated)."
            parts = [p for p in [f"{title} at {company}".strip(), loc, etype] if p]
            description = clean_text(". ".join(parts) + "." + pay_note + " " + desc)
            apply = str(item.get("job_apply_link") or item.get("job_google_link") or "")
            records.append({
                "url": apply,
                "source": "jsearch",
                "title": title[:300],
                "company": company[:300],
                "date_posted": str(item.get("job_posted_at_datetime_utc") or ""),
                "description": description,
                "description_sha256": hashlib.sha256(description.encode()).hexdigest(),
                **classify(description),
            })
            if len(records) >= limit:
                return records
    return records[:limit]


def ats_board_postings(kind: str, board: str, company: str, timeout: int = 15) -> list[dict[str, object]]:
    """Fetch public postings from a supported ATS job board.

    Only the board's own public posting API is used; no scraping of rendered
    pages. Unknown board kinds are refused so a misconfigured registry entry
    can never be silently misread. Each board gets at most one request and the
    timeout keeps one slow board from stalling the whole run.
    """
    postings: list[dict[str, object]] = []
    if kind == "greenhouse":
        data = fetch_json(f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs?content=true", timeout=timeout)
        jobs = data.get("jobs", []) if isinstance(data, dict) else []
        for job in jobs:
            if not isinstance(job, dict):
                continue
            postings.append({
                "url": str(job.get("absolute_url") or ""),
                "title": str(job.get("title") or ""),
                "company": company or str((job.get("departments") or [{}])[0].get("name", "")),
                "date_posted": str(job.get("updated_at") or ""),
                "description": clean_text(str(job.get("content") or "")),
                "evidence_text": json.dumps(job, indent=2),
            })
    elif kind == "ashby":
        data = fetch_json(f"https://api.ashbyhq.com/posting-api/job-board/{board}", timeout=timeout)
        jobs = data.get("jobs", []) if isinstance(data, dict) else []
        for job in jobs:
            if not isinstance(job, dict):
                continue
            postings.append({
                "url": str(job.get("jobUrl") or f"https://jobs.ashbyhq.com/{board}/{job.get('id', '')}"),
                "title": str(job.get("title") or ""),
                "company": company,
                "date_posted": str(job.get("publishedAt") or ""),
                "description": clean_text(str(job.get("descriptionHtml") or job.get("descriptionPlain") or "")),
                "evidence_text": json.dumps(job, indent=2),
            })
    elif kind == "lever":
        data = fetch_json(f"https://api.lever.co/v0/postings/{board}?mode=json", timeout=timeout)
        jobs = data if isinstance(data, list) else []
        for job in jobs:
            if not isinstance(job, dict):
                continue
            created = job.get("createdAt")
            postings.append({
                "url": str(job.get("hostedUrl") or ""),
                "title": str(job.get("text") or ""),
                "company": company,
                "date_posted": datetime.fromtimestamp(created / 1000, UTC).isoformat(timespec="seconds") if isinstance(created, (int, float)) else "",
                "description": clean_text(str(job.get("description") or job.get("descriptionPlain") or "")),
                "evidence_text": json.dumps(job, indent=2),
            })
    elif kind == "smartrecruiters":
        # Public postings endpoint (keyless). Listing omits full descriptions,
        # so description is synthesized from title/location/department; the
        # public job page follows the verified pattern
        # https://jobs.smartrecruiters.com/{company-identifier}/{uuid}.
        data = fetch_json(f"https://api.smartrecruiters.com/v1/companies/{board}/postings?limit=100", timeout=timeout)
        content = data.get("content", []) if isinstance(data, dict) else []
        for job in content:
            if not isinstance(job, dict):
                continue
            loc = job.get("location")
            location = str(loc.get("fullLocation") or "") if isinstance(loc, dict) else ""
            dept = job.get("department")
            department = str(dept.get("label") or "") if isinstance(dept, dict) else ""
            uuid = str(job.get("uuid") or "")
            ident = str((job.get("company") or {}).get("identifier") or board) if isinstance(job.get("company"), dict) else board
            title = str(job.get("name") or "")
            description = clean_text(" | ".join(p for p in [title, department, location] if p))
            postings.append({
                "url": f"https://jobs.smartrecruiters.com/{ident}/{uuid}" if uuid else "",
                "title": title,
                "company": company or ident,
                "date_posted": str(job.get("releasedDate") or ""),
                "description": description,
                "evidence_text": json.dumps(job, indent=2),
            })
    elif kind == "workable":
        # Public widget API (keyless). Widget listings omit descriptions, so
        # description is synthesized from title/location/department.
        data = fetch_json(f"https://apply.workable.com/api/v1/widget/accounts/{board}", timeout=timeout)
        jobs = data.get("jobs", []) if isinstance(data, dict) else []
        for job in jobs:
            if not isinstance(job, dict):
                continue
            locs = job.get("locations") or []
            location = ""
            if isinstance(locs, list) and locs and isinstance(locs[0], dict):
                first = locs[0]
                location = ", ".join(p for p in [str(first.get("city") or ""), str(first.get("country") or "")] if p)
            title = str(job.get("title") or "")
            department = str(job.get("department") or "")
            description = clean_text(" | ".join(p for p in [title, department, location] if p))
            postings.append({
                "url": str(job.get("url") or ""),
                "title": title,
                "company": company,
                "date_posted": str(job.get("published_on") or ""),
                "description": description,
                "evidence_text": json.dumps(job, indent=2),
            })
    elif kind == "personio":
        # Public XML feed (keyless). ElementTree is already imported.
        # Public job page pattern (verified): https://{board}.jobs.personio.com/job/{id}
        body, _ = fetch(f"https://{board}.jobs.personio.com/xml?language=en", timeout=timeout)
        try:
            root = ElementTree.fromstring(body)
        except ElementTree.ParseError as exc:
            raise FetchBlocked(f"personio:{board}: bad XML: {exc}") from exc
        for pos in root.iter("position"):
            def _text(el, tag):
                child = el.find(tag)
                return str(child.text or "").strip() if child is not None else ""
            pid = _text(pos, "id")
            title = _text(pos, "name") or "Untitled posting"
            office = _text(pos, "office")
            department = _text(pos, "department")
            jd = pos.find("jobDescriptions")
            jd_text = " ".join((n.text or "") for n in jd.iter()) if jd is not None else ""
            description = clean_text(" | ".join(p for p in [title, department, office] if p) + "\n" + jd_text)
            postings.append({
                "url": f"https://{board}.jobs.personio.com/job/{pid}" if pid else "",
                "title": title,
                "company": company or _text(pos, "subcompany"),
                "date_posted": _text(pos, "createdAt"),
                "description": description,
                "evidence_text": ElementTree.tostring(pos, encoding="unicode"),
            })
    else:
        raise FetchBlocked(f"unsupported ATS board kind: {kind}")
    return [p for p in postings if p["url"] and p["title"]]


def robots_sitemaps(robots_url: str, fallback: str) -> list[str]:
    text, _ = fetch(robots_url)
    found = re.findall(r"(?im)^\s*sitemap:\s*(\S+)", text)
    return found or [fallback]


def sitemap_entries(xml: str) -> tuple[list[str], list[str]]:
    """Return child sitemap URLs and leaf URLs. Namespace-agnostic by design."""
    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError as exc:
        raise FetchBlocked(f"invalid sitemap XML: {exc}") from exc
    tag = root.tag.rsplit("}", 1)[-1]
    locs = [node.text.strip() for node in root.iter() if node.tag.rsplit("}", 1)[-1] == "loc" and node.text]
    return (locs, []) if tag == "sitemapindex" else ([], locs)


def walk_sitemaps(seeds: list[str], max_urls: int = 5000) -> tuple[list[str], list[str]]:
    queue, seen, urls, blocked = list(seeds), set(), [], []
    while queue and len(urls) < max_urls:
        sitemap = queue.pop(0)
        if sitemap in seen:
            continue
        seen.add(sitemap)
        try:
            body, _ = fetch(sitemap)
            children, leaves = sitemap_entries(body)
        except FetchBlocked as exc:
            blocked.append(str(exc))
            continue
        # Career/job child maps are a smaller, more relevant denominator first.
        career_children = [u for u in children if re.search(r"job|career|position|opening", urllib.parse.urlparse(u).path, re.I)]
        queue.extend(career_children or children)
        urls.extend(leaves)
    return urls[:max_urls], blocked


def jsonld_job(raw: str) -> dict[str, object]:
    for block in re.findall(r"<script[^>]+(?:application/ld\+json|ld\+json)[^>]*>(.*?)</script>", raw, re.I | re.S):
        try:
            parsed = json.loads(block)
        except json.JSONDecodeError:
            continue
        items = parsed if isinstance(parsed, list) else parsed.get("@graph", [parsed]) if isinstance(parsed, dict) else []
        for item in items:
            if isinstance(item, dict) and item.get("@type") == "JobPosting":
                return item
    return {}


def extract_job(url: str, raw: str, source: str) -> dict[str, object]:
    item = jsonld_job(raw)
    description = clean_text(str(item.get("description") or raw))
    organization = item.get("hiringOrganization")
    company = organization.get("name", "") if isinstance(organization, dict) else ""
    title = str(item.get("title") or "")
    if not title:
        match = re.search(r"<h1[^>]*>(.*?)</h1>", raw, re.I | re.S)
        title = clean_text(match.group(1)) if match else "Untitled posting"
    return {
        "url": url,
        "source": source,
        "title": title[:300],
        "company": str(company)[:300],
        "date_posted": str(item.get("datePosted") or ""),
        "description": description,
        "description_sha256": hashlib.sha256(description.encode()).hexdigest(),
        **classify(description),
    }


def config_path(value: str) -> Path:
    return Path(value).expanduser().resolve()


def is_http_url(value: object) -> bool:
    """Accept only absolute HTTP(S) URLs from configuration or imported lists."""
    if not isinstance(value, str):
        return False
    parsed = urllib.parse.urlparse(value.strip())
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def unique_urls(urls: list[str]) -> list[str]:
    """Preserve input order while avoiding repeated evidence captures."""
    return list(dict.fromkeys(urls))


def urls_from_records(records: object, label: str) -> list[str]:
    """Extract URLs from strings or records, including hiddenjobs JSONL records."""
    if isinstance(records, dict):
        records = records.get("urls") if "urls" in records else [records]
    if isinstance(records, str):
        records = [records]
    if not isinstance(records, list):
        raise SystemExit(f"{label} must contain URL strings or objects with a url field.")
    urls: list[str] = []
    for index, record in enumerate(records, start=1):
        value = record.get("url") if isinstance(record, dict) else record
        if not is_http_url(value):
            raise SystemExit(f"{label} entry {index} is not an absolute HTTP(S) URL.")
        urls.append(str(value).strip())
    return unique_urls(urls)


def read_url_list(path: Path) -> list[str]:
    """Read JSON, JSONL, or one-URL-per-line imports without retaining other data."""
    if not path.exists():
        raise SystemExit(f"URL list not found: {path}")
    content = path.read_text().strip()
    if not content:
        return []
    try:
        return urls_from_records(json.loads(content), str(path))
    except json.JSONDecodeError:
        pass

    lines = [line.strip() for line in content.splitlines() if line.strip() and not line.lstrip().startswith("#")]
    if lines and all(line.startswith("{") for line in lines):
        try:
            return urls_from_records([json.loads(line) for line in lines], str(path))
        except json.JSONDecodeError as exc:
            raise SystemExit(f"Invalid JSONL in {path}: {exc}") from exc
    return urls_from_records(lines, str(path))


def config_relative_path(value: object, cfg_path: Path, label: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise SystemExit(f"{label} must be a non-empty file path.")
    path = Path(value).expanduser()
    return (path if path.is_absolute() else cfg_path.parent / path).resolve()


def source_direct_urls(source: dict[str, object], cfg_path: Path) -> list[str]:
    urls: list[str] = []
    if "urls" in source:
        urls.extend(urls_from_records(source["urls"], f"{source.get('name', 'source')}.urls"))
    if "url_list_file" in source:
        urls.extend(read_url_list(config_relative_path(source["url_list_file"], cfg_path, "url_list_file")))
    return unique_urls(urls)


def source_sitemap_seeds(source: dict[str, object], cfg_path: Path) -> list[str]:
    """Read optional explicit sitemap URLs; they supplement robots discovery."""
    if "sitemap_urls" not in source:
        return []
    return urls_from_records(source["sitemap_urls"], f"{source.get('name', 'source')}.sitemap_urls")


def sitemap_candidates(urls: list[str], source: dict[str, object]) -> list[str]:
    pattern = source.get("job_url_pattern")
    candidate_re = source.get("job_url_regex")
    candidates = []
    for url in urls:
        path = urllib.parse.urlparse(url).path
        if pattern and str(pattern) not in path:
            continue
        if candidate_re and not re.search(str(candidate_re), path):
            continue
        candidates.append(url)
    return unique_urls(candidates)


def load_config(path: Path) -> dict[str, object]:
    if not path.exists():
        raise SystemExit(f"Config not found: {path}. Copy config/targets.example.json to config/targets.json first.")
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid JSON in {path}: {exc}") from exc
    if not isinstance(data.get("sources"), list):
        raise SystemExit("Config must contain a sources list.")
    return data


def data_root(cfg: dict[str, object], cfg_path: Path) -> Path:
    root = Path(str(cfg.get("data_dir", ".hiddenjob")))
    root = root if root.is_absolute() else cfg_path.parent.parent / root
    root.mkdir(parents=True, exist_ok=True)
    (root / "evidence").mkdir(exist_ok=True)
    return root


def database(root: Path) -> sqlite3.Connection:
    con = sqlite3.connect(root / "hiddenjob.sqlite3")
    con.row_factory = sqlite3.Row
    con.executescript("""
    CREATE TABLE IF NOT EXISTS jobs (
      url TEXT PRIMARY KEY, source TEXT NOT NULL, title TEXT, company TEXT, date_posted TEXT,
      classification TEXT, perm_score INTEGER, lca_score INTEGER, marker_hits TEXT,
      description_sha256 TEXT, evidence_path TEXT, live INTEGER NOT NULL DEFAULT 1, last_checked TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS applications (
      job_url TEXT PRIMARY KEY REFERENCES jobs(url), state TEXT NOT NULL, note TEXT, updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS complaints (
      id INTEGER PRIMARY KEY, agency TEXT NOT NULL CHECK(agency IN ('eeoc','ofccp')),
      employer TEXT NOT NULL, contractor_status TEXT NOT NULL CHECK(contractor_status IN ('unknown','reported','verified')),
      alleged_date TEXT NOT NULL, state TEXT NOT NULL, note TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS complaint_evidence (
      complaint_id INTEGER NOT NULL REFERENCES complaints(id), job_url TEXT REFERENCES jobs(url),
      source_url TEXT NOT NULL, captured_at TEXT NOT NULL, sha256 TEXT NOT NULL, evidence_path TEXT NOT NULL
    );
    """)
    return con


def save_evidence(root: Path, url: str, content: str, suffix: str = ".html") -> Path:
    digest = hashlib.sha256(content.encode()).hexdigest()
    path = root / "evidence" / f"{digest}{suffix}"
    if not path.exists():
        path.write_text(content)
    return path


def load_ats_targets(cfg: dict[str, object], cfg_path: Path) -> list[dict[str, object]]:
    """Read the optional starter registry of careers hosts and ATS boards.

    Entries without an explicit ``enabled: false`` run their ATS board; the
    registry is a public list of boards, not evidence of any open job.
    """
    value = cfg.get("ats_targets_file")
    if not value:
        return []
    path = config_relative_path(value, cfg_path, "ats_targets_file")
    if not path.exists():
        raise SystemExit(f"ATS targets file not found: {path}")
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid JSON in {path}: {exc}") from exc
    targets = data.get("targets", [])
    if not isinstance(targets, list):
        raise SystemExit(f"{path}: expected a targets list.")
    return [t for t in targets if isinstance(t, dict)]


def host_sources(cfg: dict[str, object]) -> list[dict[str, object]]:
    """Turn explicitly enabled company_hosts entries into sitemap sources.

    Hosts default to disabled; enabling one asks sync to walk that host's own
    publisher-declared sitemaps with a bounded budget, preferring career maps.
    """
    hosts = cfg.get("company_hosts", [])
    if not isinstance(hosts, list):
        raise SystemExit("company_hosts must be a list.")
    sources = []
    for entry in hosts:
        if not isinstance(entry, dict) or entry.get("enabled") is not True:
            continue
        host = str(entry.get("host", "")).strip()
        company = str(entry.get("company", "")).strip()
        if not host:
            raise SystemExit("Every enabled company_hosts entry needs a host.")
        sources.append({
            "name": f"host:{host}",
            "company": company,
            "robots_url": f"https://{host}/robots.txt",
            "sitemap_fallback": f"https://{host}/sitemap.xml",
            "job_url_regex": r"(?i)/(?:[^?/]*/)*(?:job|career|position|opening|vacanc)[^?]*",
            "host_sitemap": True,
        })
    return sources


def store_job(con: sqlite3.Connection, root: Path, record: dict[str, object]) -> None:
    """Classify a posting record, persist its evidence, and upsert the ledger."""
    record = dict(record)
    record.update(classify(str(record.get("description") or "")))
    suffix = ".json" if record.pop("evidence_is_json", False) else ".html"
    evidence = save_evidence(root, str(record["url"]), str(record.pop("evidence_text", "")), suffix=suffix)
    con.execute("""INSERT INTO jobs(url,source,title,company,date_posted,classification,perm_score,lca_score,marker_hits,description_sha256,evidence_path,live,last_checked)
      VALUES(:url,:source,:title,:company,:date_posted,:classification,:perm_score,:lca_score,:marker_hits,:description_sha256,:evidence_path,1,:last_checked)
      ON CONFLICT(url) DO UPDATE SET source=excluded.source,title=excluded.title,company=excluded.company,date_posted=excluded.date_posted,classification=excluded.classification,perm_score=excluded.perm_score,lca_score=excluded.lca_score,marker_hits=excluded.marker_hits,description_sha256=excluded.description_sha256,evidence_path=excluded.evidence_path,live=1,last_checked=excluded.last_checked""",
      {**record, "marker_hits": json.dumps(record["marker_hits"]),
       "description_sha256": hashlib.sha256(str(record.get("description") or "").encode()).hexdigest(),
       "evidence_path": str(evidence), "last_checked": now()})


def cmd_init(args: argparse.Namespace) -> int:
    destination = config_path(args.config)
    if destination.exists() and not args.force:
        raise SystemExit(f"Refusing to overwrite {destination}; pass --force to replace it.")
    example = Path(__file__).parent / "config" / "targets.example.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(example.read_text())
    print(f"Created {destination}. Review it before running a sync.")
    return 0


def cmd_sync(args: argparse.Namespace) -> int:
    cfg_path = config_path(args.config)
    cfg, root = load_config(cfg_path), None
    root = data_root(cfg, cfg_path)
    con = database(root)
    sources = list(cfg["sources"]) + host_sources(cfg)
    for source in sources:
        if not isinstance(source, dict) or not isinstance(source.get("name"), str) or not source["name"].strip():
            raise SystemExit("Every source needs a non-empty name.")
        name = str(source["name"])
        # API-based sources bypass the sitemap pipeline. Each fetcher returns
        # records in extract_job()'s shape so they flow through the normal
        # store/classify/rank pipeline.
        feed_fetchers = {
            "hackernews": hackernews_jobs,
            "remoteok": remoteok_jobs,
            "remotive": remotive_jobs,
            "arbeitnow": arbeitnow_jobs,
        }
        kind = str(source.get("kind") or "")
        if kind in feed_fetchers:
            feed_limit = int(source.get("limit") or args.limit or 50)
            records = feed_fetchers[kind](limit=feed_limit)
            for record in records:
                record["evidence_text"] = record.get("description", "")
                store_job(con, root, record)
            con.commit()
            print(f"{name}: api={kind}, captured={len(records)}")
            continue
        if kind == "usajobs":
            # USAJOBS needs a free API key (https://developer.usajobs.gov/apirequest/).
            # Read from the env var named by api_key_env, falling back to
            # ~/.config/hiddenjob.env (chmod 600); never hardcoded.
            env_name = str(source.get("api_key_env") or "USAJOBS_API_KEY")
            api_key = os.environ.get(env_name, "") or _secret_from_file(env_name)
            ua_email = os.environ.get("USAJOBS_USER_AGENT_EMAIL", "") or _secret_from_file("USAJOBS_USER_AGENT_EMAIL")
            if not api_key:
                print(f"{name}: no USAJOBS API key in ${env_name}; skipping source, preserving existing rows.", file=sys.stderr)
                continue
            uj_limit = int(source.get("limit") or args.limit or 50)
            records = usajobs_jobs(limit=uj_limit, api_key=api_key, keyword=str(source.get("keyword") or ""),
                                 user_agent_email=ua_email)
            for record in records:
                record["evidence_text"] = record.get("description", "")
                store_job(con, root, record)
            con.commit()
            print(f"{name}: api=usajobs, captured={len(records)}")
            continue
        if kind == "adzuna":
            # Adzuna: free app_id/app_key (https://developer.adzuna.com/signup).
            # Read from env, falling back to ~/.config/hiddenjob.env (chmod 600).
            app_id = os.environ.get("ADZUNA_APP_ID", "") or _secret_from_file("ADZUNA_APP_ID")
            app_key = os.environ.get("ADZUNA_APP_KEY", "") or _secret_from_file("ADZUNA_APP_KEY")
            if not app_id or not app_key:
                print(f"{name}: no Adzuna credentials (ADZUNA_APP_ID/ADZUNA_APP_KEY); skipping source, preserving existing rows.", file=sys.stderr)
                continue
            az_limit = int(source.get("limit") or args.limit or 50)
            records = adzuna_jobs(limit=az_limit, app_id=app_id, app_key=app_key,
                                  keyword=str(source.get("keyword") or ""),
                                  location=str(source.get("location") or ""))
            for record in records:
                record["evidence_text"] = record.get("description", "")
                store_job(con, root, record)
            con.commit()
            print(f"{name}: api=adzuna, captured={len(records)}")
            continue
        if kind == "usajobs_historic":
            # Keyless: data.usajobs.gov/api/historicjoa needs no API key.
            uj_limit = int(source.get("limit") or args.limit or 200)
            records = usajobs_historic_jobs(
                limit=uj_limit,
                days_back=int(source.get("days_back") or 3),
                keyword=str(source.get("keyword") or ""))
            for record in records:
                record["evidence_text"] = record.get("description", "")
                store_job(con, root, record)
            con.commit()
            print(f"{name}: api=usajobs_historic, captured={len(records)}")
            continue
            for record in records:
                record["evidence_text"] = record.get("description", "")
                store_job(con, root, record)
            con.commit()
        if kind == "jsearch":
            # JSearch (RapidAPI): key in X-RapidAPI-Key header. Free tier
            # ~200 req/month, so pages are capped inside jsearch_jobs.
            env_name = str(source.get("api_key_env") or "JSEARCH_API_KEY")
            js_key = os.environ.get(env_name, "") or _secret_from_file(env_name)
            if not js_key:
                print(f"{name}: no JSearch API key in ${env_name}; skipping source, preserving existing rows.", file=sys.stderr)
                continue
            js_limit = int(source.get("limit") or args.limit or 30)
            records = jsearch_jobs(limit=js_limit, api_key=js_key,
                                   keyword=str(source.get("keyword") or ""),
                                   location=str(source.get("location") or ""))
            for record in records:
                record["evidence_text"] = record.get("description", "")
                store_job(con, root, record)
            con.commit()
            print(f"{name}: api=jsearch, captured={len(records)}")
            continue
        direct_urls = source_direct_urls(source, cfg_path)
        seeds = source_sitemap_seeds(source, cfg_path)
        blocked: list[str] = []
        if "robots_url" in source:
            if "sitemap_fallback" not in source:
                raise SystemExit(f"{name}: robots_url requires sitemap_fallback.")
            if not is_http_url(source["robots_url"]) or not is_http_url(source["sitemap_fallback"]):
                raise SystemExit(f"{name}: robots_url and sitemap_fallback must be absolute HTTP(S) URLs.")
            try:
                seeds.extend(robots_sitemaps(str(source["robots_url"]), str(source["sitemap_fallback"])))
            except FetchBlocked as exc:
                blocked.append(str(exc))
        if not direct_urls and not seeds:
            if blocked:
                print(f"{name}: all seeds blocked ({'; '.join(blocked)}); skipping source, preserving existing rows.", file=sys.stderr)
                continue
            raise SystemExit(f"{name}: provide urls, url_list_file, sitemap_urls, or robots_url with sitemap_fallback.")
        budget = getattr(args, "max_host_sitemap_urls", 400) if source.get("host_sitemap") else args.max_sitemap_urls
        sitemap_urls, sitemap_blocked = walk_sitemaps(unique_urls(seeds), budget) if seeds else ([], [])
        blocked.extend(sitemap_blocked)
        candidates = unique_urls(direct_urls + sitemap_candidates(sitemap_urls, source))
        if not candidates:
            print(f"{name}: no matching job URLs; not treating this as a negative finding.", file=sys.stderr)
        if blocked:
            print(f"{name}: {len(blocked)} sitemap(s) could not be measured; preserving existing rows.", file=sys.stderr)
        fetched = 0
        # Per-source limit override: high-volume aggregators (e.g. jobs.now) can
        # run deeper than the global --limit without slowing small boards.
        try:
            source_limit = int(source.get("limit") or args.limit)
        except (TypeError, ValueError):
            source_limit = args.limit
        # take: "tail" — for publishers whose sitemap lists oldest-first
        # (e.g. jobs.now numeric IDs ascend with newness), capture the newest
        # candidates instead of the oldest.
        take = str(source.get("take") or "head").lower()
        selected = candidates[-source_limit:] if take == "tail" and source_limit > 0 else candidates[:source_limit]
        for url in selected:
            try:
                raw, final_url = fetch(url)
            except FetchBlocked as exc:
                print(f"skip: {exc}", file=sys.stderr)
                continue
            if not raw:
                print(f"skip: {url}: HTTP 404; no evidence captured", file=sys.stderr)
                continue
            record = extract_job(final_url, raw, name)
            record["evidence_text"] = raw
            store_job(con, root, record)
            fetched += 1
        con.commit()
        print(f"{name}: declared_urls={len(sitemap_urls)}, direct_urls={len(direct_urls)}, candidates={len(candidates)}, captured={fetched}, blocked_maps={len(blocked)}, truncated={len(candidates) > source_limit}")
    ats_targets = load_ats_targets(cfg, cfg_path)
    max_boards = getattr(args, "ats_max_boards", 0) or 0
    ats_timeout = getattr(args, "ats_timeout", 15) or 15
    boards_seen = 0
    for target in ats_targets:
        if max_boards and boards_seen >= max_boards:
            print(f"ats: board cap reached ({max_boards}); {len(ats_targets) - boards_seen} board(s) deferred.", file=sys.stderr)
            break
        ats = target.get("ats")
        company = str(target.get("company", ""))
        if not isinstance(ats, dict) or target.get("enabled") is False:
            continue
        kind, board = str(ats.get("kind", "")), str(ats.get("board", ""))
        if not kind or not board:
            print(f"ats:{company}: missing kind or board; skipping.", file=sys.stderr)
            continue
        label = f"ats:{kind}:{board}"
        boards_seen += 1
        try:
            postings = ats_board_postings(kind, board, company, timeout=ats_timeout)
        except FetchBlocked as exc:
            print(f"{label}: skipped: {exc}", file=sys.stderr)
            continue
        captured = 0
        try:
            target_limit = int(target.get("limit") or args.limit)
        except (TypeError, ValueError):
            target_limit = args.limit
        # Optional per-target keyword filter: when present, only postings
        # whose title contains one of the keywords (case-insensitive) are
        # stored. Boards can list hundreds of roles; without this the limit
        # would truncate before the relevant ones are seen.
        keywords = [k.strip().lower() for k in (target.get("keywords") or [])
                    if isinstance(k, str) and k.strip()]
        matched = postings
        if keywords:
            matched = [p for p in postings
                       if any(k in str(p.get("title") or "").lower() for k in keywords)]
        for posting in matched[:target_limit]:
            posting = dict(posting)
            posting["source"] = label
            posting["evidence_is_json"] = True
            store_job(con, root, posting)
            captured += 1
        con.commit()
        print(f"{label} ({company or 'unknown company'}): board_postings={len(postings)}, matched={len(matched)}, captured={captured}, truncated={len(matched) > target_limit}")
    return 0


def rows(con: sqlite3.Connection, sql: str, params: tuple = ()) -> list[dict[str, object]]:
    return [dict(row) for row in con.execute(sql, params)]


def with_db(args: argparse.Namespace) -> tuple[sqlite3.Connection, Path]:
    cp = config_path(args.config)
    cfg = load_config(cp)
    root = data_root(cfg, cp)
    return database(root), root


def cmd_jobs(args: argparse.Namespace) -> int:
    con, _ = with_db(args)
    where, params = ["live=1"], []
    if args.classification:
        where.append("classification=?"); params.append(args.classification)
    if args.company:
        where.append("company LIKE ?"); params.append(f"%{args.company}%")
    data = rows(con, "SELECT url,company,title,classification,perm_score,lca_score,date_posted,last_checked FROM jobs WHERE " + " AND ".join(where) + " ORDER BY perm_score DESC,lca_score DESC,title LIMIT ?", tuple(params + [args.limit]))
    print(json.dumps(data, indent=2))
    return 0


def cmd_stage(args: argparse.Namespace) -> int:
    con, _ = with_db(args)
    exists = con.execute("SELECT 1 FROM jobs WHERE url=?", (args.url,)).fetchone()
    if not exists:
        raise SystemExit("That URL is not in the local evidence ledger. Sync and review the posting first.")
    con.execute("INSERT INTO applications(job_url,state,note,updated_at) VALUES(?,?,?,?) ON CONFLICT(job_url) DO UPDATE SET state=excluded.state,note=excluded.note,updated_at=excluded.updated_at", (args.url, "staged", args.note or "", now()))
    con.commit()
    print("Staged for human review. No application was submitted or sent.")
    return 0


def cmd_track(args: argparse.Namespace) -> int:
    allowed = {"staged", "drafted", "submitted", "interview", "offer", "rejected", "withdrawn", "closed"}
    if args.state not in allowed:
        raise SystemExit(f"state must be one of: {', '.join(sorted(allowed))}")
    con, _ = with_db(args)
    if not con.execute("SELECT 1 FROM applications WHERE job_url=?", (args.url,)).fetchone():
        raise SystemExit("Stage the job first so the record has a reviewed source posting.")
    con.execute("UPDATE applications SET state=?,note=?,updated_at=? WHERE job_url=?", (args.state, args.note or "", now(), args.url))
    con.commit(); print(f"Tracked {args.state}. Record whether you submitted only after an actual confirmation.")
    return 0


def cmd_check_duplicate(args: argparse.Namespace) -> int:
    """Check if a job URL (or company+title) already has an application record."""
    import json
    con, _ = with_db(args)
    # Exact URL match
    app = con.execute(
        "SELECT a.job_url, a.state, a.updated_at, j.company, j.title FROM applications a LEFT JOIN jobs j ON j.url=a.job_url WHERE a.job_url=?",
        (args.url,)).fetchone()
    if app:
        print(json.dumps({"duplicate": True, "match_type": "exact_url", "job_url": app["job_url"],
                          "state": app["state"], "company": app["company"], "title": app["title"],
                          "updated_at": app["updated_at"]}, indent=2))
        return 0
    # Fuzzy: same company + similar title (if --company and --title provided)
    if args.company and args.title:
        rows = con.execute(
            """SELECT a.job_url, a.state, j.company, j.title FROM applications a
               JOIN jobs j ON j.url=a.job_url
               WHERE LOWER(j.company)=LOWER(?) AND LOWER(j.title) LIKE '%' || LOWER(?) || '%'""",
            (args.company, args.title)).fetchall()
        if rows:
            print(json.dumps({"duplicate": True, "match_type": "company_title",
                              "matches": [{"job_url": r["job_url"], "state": r["state"],
                                          "company": r["company"], "title": r["title"]} for r in rows]}, indent=2))
            return 0
    print(json.dumps({"duplicate": False}, indent=2))
    return 0


def cmd_apply(args: argparse.Namespace) -> int:
    """Run the optional local browser adapter against a reviewed, staged job."""
    con, root = with_db(args)
    app = con.execute("SELECT state FROM applications WHERE job_url=?", (args.url,)).fetchone()
    if app and app["state"] == "submitted":
        raise SystemExit(f"Already submitted for {args.url}; refusing to apply twice. Use 'review' to see the submission record.")
    if app and app["state"] == "prefilled":
        raise SystemExit(f"Already prefilled for {args.url}; review before submitting. Use 'review' to see the prefill record.")
    if not app or app["state"] not in {"staged", "drafted"}:
        raise SystemExit("Stage and review the job before using the browser adapter.")
    profile = Path(args.profile).expanduser()
    if not profile.exists():
        raise SystemExit(f"Private profile not found: {profile}")
    if args.submit and not args.confirm_submit:
        raise SystemExit("Submitting also requires --confirm-submit; prefill is the default.")
    if args.submit and __import__("os").environ.get("HIDDENJOB_AUTOSUBMIT") != "1":
        raise SystemExit("Submitting also requires HIDDENJOB_AUTOSUBMIT=1 in this process environment.")
    folder = root / "applications" / hashlib.sha256(args.url.encode()).hexdigest()[:16]
    folder.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, str(Path(__file__).with_name("ats_prefill.py")), "--url", args.url,
           "--profile", str(profile), "--evidence-dir", str(folder)]
    if args.submit:
        cmd.append("--submit")
    if args.dry_run:
        cmd.append("--dry-run")
    result = subprocess.run(cmd, text=True, capture_output=True)
    if result.stderr:
        print(result.stderr, file=sys.stderr, end="")
    if result.returncode not in (0, 2):
        raise SystemExit(result.returncode)
    print(result.stdout, end="")
    result_path = folder / "prefill-result.json"
    if args.submit and result_path.exists():
        outcome = json.loads(result_path.read_text()).get("outcome")
        if outcome == "submitted":
            con.execute("UPDATE applications SET state='submitted', updated_at=? WHERE job_url=?", (now(), args.url))
            con.commit()
    return result.returncode


def cmd_apply_staged(args: argparse.Namespace) -> int:
    """Process a bounded batch that the person already staged and reviewed."""
    con, _ = with_db(args)
    sql = "SELECT a.job_url FROM applications a JOIN jobs j ON j.url=a.job_url WHERE a.state IN ('staged','drafted')"
    params: list[object] = []
    if args.classification:
        sql += " AND j.classification=?"; params.append(args.classification)
    jobs = rows(con, sql + " ORDER BY a.updated_at LIMIT ?", tuple(params + [args.limit]))
    if not jobs:
        print("No staged jobs match the requested batch.")
        return 0
    outcomes = []
    for row in jobs:
        per_job = argparse.Namespace(config=args.config, url=row["job_url"], profile=args.profile,
                                     submit=args.submit, confirm_submit=args.confirm_submit, dry_run=args.dry_run)
        try:
            code = cmd_apply(per_job)
        except SystemExit as exc:
            code = int(exc.code) if isinstance(exc.code, int) else 1
        outcomes.append({"url": row["job_url"], "exit_code": code})
    print(json.dumps({"processed": len(outcomes), "outcomes": outcomes}, indent=2))
    return 0 if all(x["exit_code"] == 0 for x in outcomes) else 2


def complaint_guidance(agency: str, alleged: date) -> dict[str, object]:
    elapsed = (date.today() - alleged).days
    if agency == "ofccp":
        return {"agency": "DOL OFCCP", "deadline": "300 calendar days from the alleged action", "days_elapsed": elapsed,
                "route": "Pre-complaint inquiry or complaint: https://www.dol.gov/agencies/ofccp/contact/file-complaint",
                "scope": "Federal-contractor/subcontractor employment discrimination based on disability or protected-veteran status; agency fit is not determined by this tool."}
    return {"agency": "EEOC", "deadline": "Usually 180 days; often 300 days when an applicable state/local agency enforces a parallel law", "days_elapsed": elapsed,
            "route": "Public Portal and charge process: https://www.eeoc.gov/how-file-charge-employment-discrimination",
            "scope": "Protected-basis employment discrimination; federal-contractor status alone does not establish an EEOC claim."}


def cmd_complaint_create(args: argparse.Namespace) -> int:
    try:
        alleged = date.fromisoformat(args.alleged_date)
    except ValueError as exc:
        raise SystemExit("--alleged-date must be YYYY-MM-DD") from exc
    con, _ = with_db(args)
    stamp = now()
    cur = con.execute("INSERT INTO complaints(agency,employer,contractor_status,alleged_date,state,note,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)", (args.agency, args.employer, args.contractor_status, alleged.isoformat(), "draft", args.note or "", stamp, stamp))
    con.commit()
    out = {"case_id": cur.lastrowid, "filing_state": "draft", "not_filed": True, **complaint_guidance(args.agency, alleged)}
    print(json.dumps(out, indent=2))
    return 0


def cmd_complaint_evidence(args: argparse.Namespace) -> int:
    con, root = with_db(args)
    case = con.execute("SELECT id FROM complaints WHERE id=?", (args.case,)).fetchone()
    job = con.execute("SELECT url,evidence_path,description_sha256 FROM jobs WHERE url=?", (args.url,)).fetchone()
    if not case or not job:
        raise SystemExit("Case or job URL not found. Capture the posting through sync before attaching it.")
    con.execute("INSERT INTO complaint_evidence(complaint_id,job_url,source_url,captured_at,sha256,evidence_path) VALUES(?,?,?,?,?,?)", (args.case, args.url, args.url, now(), job["description_sha256"], job["evidence_path"]))
    con.commit(); print("Evidence linked to draft case. This does not file or transmit anything.")
    return 0


def cmd_complaint_list(args: argparse.Namespace) -> int:
    con, _ = with_db(args)
    print(json.dumps(rows(con, "SELECT id,agency,employer,contractor_status,alleged_date,state,created_at,updated_at FROM complaints ORDER BY id DESC"), indent=2))
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    con, _ = with_db(args)
    out = {
        "jobs_by_classification": rows(con, "SELECT classification,count(*) AS count FROM jobs WHERE live=1 GROUP BY classification ORDER BY count DESC"),
        "application_states": rows(con, "SELECT state,count(*) AS count FROM applications GROUP BY state ORDER BY count DESC"),
        "complaint_cases": rows(con, "SELECT agency,state,count(*) AS count FROM complaints GROUP BY agency,state ORDER BY agency,state"),
        "notice": "Records are local. Complaint cases remain drafts until the person filing submits through the official agency process.",
    }
    print(json.dumps(out, indent=2)); return 0


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--config", default="config/targets.json", help="private runtime config path")
    sub = p.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init"); init.add_argument("--force", action="store_true"); init.set_defaults(func=cmd_init)
    sync = sub.add_parser("sync"); sync.add_argument("--limit", type=int, default=25); sync.add_argument("--max-sitemap-urls", type=int, default=5000); sync.add_argument("--max-host-sitemap-urls", type=int, default=400); sync.add_argument("--ats-timeout", type=int, default=15, help="seconds per ATS board request"); sync.add_argument("--ats-max-boards", type=int, default=0, help="cap ATS boards per run; 0 = all"); sync.set_defaults(func=cmd_sync)
    jobs = sub.add_parser("jobs"); jobs.add_argument("--classification"); jobs.add_argument("--company"); jobs.add_argument("--limit", type=int, default=50); jobs.set_defaults(func=cmd_jobs)
    stage = sub.add_parser("stage"); stage.add_argument("url"); stage.add_argument("--note"); stage.set_defaults(func=cmd_stage)
    track = sub.add_parser("track"); track.add_argument("url"); track.add_argument("state"); track.add_argument("--note"); track.set_defaults(func=cmd_track)
    dup = sub.add_parser("check-duplicate", help="check if a job already has an application record"); dup.add_argument("url"); dup.add_argument("--company"); dup.add_argument("--title"); dup.set_defaults(func=cmd_check_duplicate)
    apply = sub.add_parser("apply", help="optional browser prefill for a staged job")
    apply.add_argument("url"); apply.add_argument("--profile", default="config/profile.json")
    apply.add_argument("--submit", action="store_true", help="click submit only with the environment and confirmation gates")
    apply.add_argument("--confirm-submit", action="store_true", help="acknowledge that a real submission is requested")
    apply.add_argument("--dry-run", action="store_true", help="validate private profile and planned action without opening a browser")
    apply.set_defaults(func=cmd_apply)
    batch = sub.add_parser("apply-staged", help="bounded prefill batch for already-staged jobs")
    batch.add_argument("--profile", default="config/profile.json"); batch.add_argument("--classification")
    batch.add_argument("--limit", type=int, default=3); batch.add_argument("--submit", action="store_true")
    batch.add_argument("--confirm-submit", action="store_true"); batch.add_argument("--dry-run", action="store_true")
    batch.set_defaults(func=cmd_apply_staged)
    complaint = sub.add_parser("complaint"); csub = complaint.add_subparsers(dest="complaint_command", required=True)
    cc = csub.add_parser("create"); cc.add_argument("--agency", required=True, choices=("eeoc", "ofccp")); cc.add_argument("--employer", required=True); cc.add_argument("--alleged-date", required=True); cc.add_argument("--contractor-status", choices=("unknown", "reported", "verified"), default="unknown"); cc.add_argument("--note"); cc.set_defaults(func=cmd_complaint_create)
    ce = csub.add_parser("evidence"); ce.add_argument("--case", type=int, required=True); ce.add_argument("--url", required=True); ce.set_defaults(func=cmd_complaint_evidence)
    cl = csub.add_parser("list"); cl.set_defaults(func=cmd_complaint_list)
    report = sub.add_parser("report"); report.set_defaults(func=cmd_report)
    return p


def main() -> int:
    args = parser().parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
