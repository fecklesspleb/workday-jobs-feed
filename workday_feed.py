#!/usr/bin/env python3
"""Daily feed of new jobs from Workday career sites, written to CSV.

Two sources:
  public  Reads each site's public CXS JSON endpoint (the one the career page itself calls).
          Free, standard library only. Splits large sites by facet to get past the
          2,000-result ceiling of a single query.
  apify   Runs the fecklesspleb/workday-jobs-scraper Actor through the Apify API
          (run-sync-get-dataset-items) with a hard spend cap. Needs APIFY_TOKEN.

Both sources write the same columns. Every job gets a stable key, uid = "workday:<tenant>:<job id>",
and a job is new when its uid is not already in data/jobs.csv.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

USER_AGENT = os.environ.get(
    "JOBS_FEED_USER_AGENT",
    "workday-jobs-feed/1.0 (+https://github.com/fecklesspleb/workday-jobs-feed)",
)
PAGE = 20      # the largest page size the jobs endpoint accepts
CAP = 2000     # the most results one query will page through
MAX_DEPTH = 3  # how many facets may be combined to split a site under the cap
APIFY_ACTOR = "fecklesspleb~workday-jobs-scraper"
APIFY_BASE = "https://api.apify.com/v2"
COLUMNS = ["uid", "site", "job_id", "title", "location", "posted", "company_name", "url", "first_seen"]


# ---------------------------------------------------------------- HTTP

def http(url: str, body: dict | None = None, accept: str = "application/json", timeout: float = 30,
         headers: dict | None = None) -> bytes:
    """GET (body is None) or POST JSON. Returns the response bytes; raises urllib.error.HTTPError."""
    h = {"User-Agent": USER_AGENT, "Accept": accept}
    if body is not None:
        h["Content-Type"] = "application/json"
    h.update(headers or {})
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=h, method="POST" if data is not None else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


# ---------------------------------------------------------------- sites

class Site:
    def __init__(self, url: str, host: str, tenant: str, name: str):
        self.url, self.host, self.tenant, self.name = url, host, tenant, name
        self.api = f"https://{host}/wday/cxs/{tenant}/{name}"
        self.key = f"{tenant}/{name}"


LOCALE = re.compile(r"^[a-z]{2}(-[A-Za-z]{2,4})?$")


def parse_site(url: str) -> Site:
    """Accepts https://<tenant>.wdN.myworkdayjobs.com/[locale/]<site>
    and https://wdN.myworkdaysite.com/[locale/]recruiting/<tenant>/<site>."""
    u = urllib.parse.urlsplit(url.strip() if "://" in url else "https://" + url.strip())
    host = (u.hostname or "").lower()
    parts = [p for p in u.path.split("/") if p and not LOCALE.match(p)]
    m = re.fullmatch(r"([\w-]+)\.(wd\d+)\.myworkdayjobs\.com", host)
    if m:
        if not parts:
            raise ValueError(f"{url}: the URL needs the site name after the host")
        return Site(url, host, m.group(1), parts[0])
    m = re.fullmatch(r"(wd\d+)\.myworkdaysite\.com", host)
    if m and len(parts) >= 3 and parts[0] == "recruiting":
        tenant = parts[1].lower()
        # Same site and API as the tenant's myworkdayjobs.com host, whose robots.txt lists its sites.
        return Site(url, f"{tenant}.{m.group(1)}.myworkdayjobs.com", tenant, parts[2])
    raise ValueError(f"{url}: not a Workday career site URL (myworkdayjobs.com or myworkdaysite.com/recruiting/...)")


def read_sites(path: Path) -> list[str]:
    out = []
    for ln in path.read_text(encoding="utf-8").splitlines():
        ln = re.split(r"(?:^|\s)#", ln, maxsplit=1)[0].strip()  # drop comments
        if ln:
            out.append(ln)
    return out


def site_allowed(site: Site, transport=http) -> bool:
    """Is /<site>/ allowed in the tenant's robots.txt? The API path is never listed there,
    so the useful check is on the site path. Ask for text/plain: some tenants answer an
    Accept header that prefers HTML with the app shell instead of the robots file."""
    try:
        text = transport(f"https://{site.host}/robots.txt", accept="text/plain").decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        if 400 <= e.code < 500:
            return True  # RFC 9309: no robots.txt means no restrictions
        raise
    rp = urllib.robotparser.RobotFileParser()
    rp.parse(text.splitlines())
    return rp.can_fetch(USER_AGENT, f"https://{site.host}/{site.name}/")


# ---------------------------------------------------------------- reading one site

class BudgetExceeded(Exception):
    pass


def job_id(posting: dict) -> str | None:
    """The requisition ID. bulletFields usually holds it, but not always first (Intel puts
    'Spotlight Job' there), and externalPath can end in a suffix such as JR2028745-1."""
    path = posting.get("externalPath")
    if not path:
        return None
    tail = path.rsplit("_", 1)[-1]
    for b in posting.get("bulletFields") or []:
        if isinstance(b, str) and (tail == b or tail.startswith(b + "-")):
            return b
    return tail


class WorkdayReader:
    def __init__(self, site: Site, transport=http, delay: float = 0.25, max_requests: int = 500, cap: int = CAP):
        self.site, self.transport, self.delay = site, transport, delay
        self.max_requests, self.cap = max_requests, cap
        self.requests = 0
        self.partial: list[str] = []   # why the read may have missed jobs
        self.pathless = 0              # list entries with no job page

    def _post(self, body: dict) -> dict:
        if self.requests >= self.max_requests:
            raise BudgetExceeded(f"stopped after {self.requests} requests (--max-requests)")
        if self.requests and self.delay:
            time.sleep(self.delay)
        self.requests += 1
        return json.loads(self.transport(self.site.api + "/jobs", body=body))

    def search(self, facets: dict, offset: int = 0) -> dict:
        return self._post({"appliedFacets": facets, "limit": PAGE, "offset": offset, "searchText": ""})

    def page_through(self, facets: dict, first: dict):
        total = first.get("total") or 0  # only the first page carries the real total
        yield from first.get("jobPostings") or []
        for offset in range(PAGE, min(total, self.cap), PAGE):
            yield from self.search(facets, offset).get("jobPostings") or []

    def pick_facet(self, first: dict, applied: dict):
        """A facet whose buckets all have counts and add up to at least the total. Prefer the one
        with the smallest largest bucket. A distance facet (count 1 per bucket) fails the sum test."""
        total = first.get("total") or 0
        best = None
        for f in first.get("facets") or []:
            param, values = f.get("facetParameter"), f.get("values") or []
            if not param or param in applied or not values:
                continue
            counts = [v.get("count") for v in values]
            if any(not isinstance(c, int) for c in counts) or sum(counts) < total:
                continue
            if best is None or max(counts) < best[0]:
                best = (max(counts), param, values)
        return best

    def cover(self, facets: dict, first: dict, depth: int = 0):
        """Yield every posting matching `facets`, splitting by facet while a query hits the cap."""
        if (first.get("total") or 0) < self.cap:
            yield from self.page_through(facets, first)
            return
        best = self.pick_facet(first, facets) if depth < MAX_DEPTH else None
        if best is None:
            self.partial.append(f"no facet splits {json.dumps(facets) if facets else 'the site'} under {self.cap}")
            yield from self.page_through(facets, first)
            return
        _, param, values = best
        for v in values:
            if not v.get("count"):
                continue
            sub = {**facets, param: [v["id"]]}
            yield from self.cover(sub, self.search(sub), depth + 1)

    def jobs(self):
        """Distinct postings for the site, deduplicated by job id (a posting can appear in several
        facet buckets, and can move between buckets while a long read is running)."""
        seen = set()
        try:
            for p in self.cover({}, self.search({})):
                jid = job_id(p)
                if jid is None:
                    self.pathless += 1
                    continue
                if jid in seen:
                    continue
                seen.add(jid)
                yield jid, p
        except BudgetExceeded as e:
            self.partial.append(str(e))

    def detail(self, posting: dict) -> dict:
        if self.delay:
            time.sleep(self.delay)
        self.requests += 1
        return json.loads(self.transport(self.site.api + posting["externalPath"]))


def approx_posted(label: str | None, today: date) -> str | None:
    """'Posted Today' / 'Posted Yesterday' / 'Posted 3 Days Ago' -> an ISO date.
    '30+ Days Ago' is only a lower bound, so it gives None. The label's time zone is unknown,
    so near midnight the result can be a day off."""
    t = (label or "").lower()
    if "+" in t:
        return None
    if "today" in t:
        return today.isoformat()
    if "yesterday" in t:
        return (today - timedelta(days=1)).isoformat()
    m = re.search(r"(\d+)\s+days?\s+ago", t)
    return (today - timedelta(days=int(m.group(1)))).isoformat() if m else None


def strip_entity_code(name: str | None) -> str | None:
    """'2100 NVIDIA USA' -> 'NVIDIA USA'."""
    return re.sub(r"^\d+\s+", "", name).strip() if name else None


def row_from_posting(site: Site, jid: str, p: dict, today: date) -> dict:
    return {
        "uid": f"workday:{site.tenant}:{jid}", "site": site.key, "job_id": jid,
        "title": (p.get("title") or "").strip(), "location": p.get("locationsText"),
        "posted": approx_posted(p.get("postedOn"), today), "company_name": None,
        "url": f"https://{site.host}/{site.name}{p['externalPath']}",
    }


def add_details(row: dict, det: dict) -> dict:
    info = det.get("jobPostingInfo") or {}
    locs = [info.get("location")] + list(info.get("additionalLocations") or [])
    return {**row,
            "location": "; ".join(x for x in locs if x) or row["location"],
            "posted": info.get("startDate") or row["posted"],
            "company_name": strip_entity_code((det.get("hiringOrganization") or {}).get("name"))}


def fetch_public(urls, keywords, today, transport=http, delay=0.25, max_requests=500, log=print):
    """Read every site. Returns (rows, readers_by_uid, errors)."""
    rows, postings, errors = [], {}, []
    for url in urls:
        try:
            site = parse_site(url)
            if not site_allowed(site, transport):
                errors.append(f"{url}: robots.txt disallows /{site.name}/, skipped")
                continue
            r = WorkdayReader(site, transport, delay=delay, max_requests=max_requests)
            n = 0
            for jid, p in r.jobs():
                n += 1
                row = row_from_posting(site, jid, p, today)
                if keep(row, keywords):
                    rows.append(row)
                    postings[row["uid"]] = (r, p)
            msg = f"{site.key}: {n} jobs, {r.requests} requests"
            if r.pathless:
                msg += f", {r.pathless} entries without a job page skipped"
            log(msg)
            for why in r.partial:
                log(f"warning: {site.key}: {why}; some jobs may be missing this run")
        except (ValueError, urllib.error.URLError, json.JSONDecodeError, KeyError) as e:
            errors.append(f"{url}: {e}")
    return rows, postings, errors


def keep(row: dict, keywords: list[str]) -> bool:
    if not keywords:
        return True
    t = row["title"].lower()
    return any(k.lower() in t for k in keywords)


# ---------------------------------------------------------------- Apify Actor

def is_job_item(item: dict) -> bool:
    """The Actor can also write free records: a status message (type 'status') and, with
    includeExpired, expired jobs (status 'expired'). Keep only job records."""
    if item.get("type") == "status" or item.get("status") not in (None, "active"):
        return False
    return bool(item.get("uid") and item.get("url") and item.get("title"))


def from_actor_item(item: dict) -> dict:
    # The Actor's company field is companyName (the hiring entity without its code), not company.
    return {
        "uid": item["uid"], "site": item.get("companySlug"), "job_id": item.get("id"),
        "title": (item.get("title") or "").strip(), "location": item.get("location"),
        "posted": item.get("postedAt"), "company_name": item.get("companyName"), "url": item.get("url"),
    }


def actor_input(urls, keywords, state_store="workday-jobs-feed", max_jobs=500) -> dict:
    body = {"companies": urls, "onlyNewJobs": True, "stateStoreName": state_store,
            "includeDescription": False, "maxTotalJobs": max_jobs}
    if keywords:
        body["keywords"] = keywords
    return body


def run_actor(body: dict, token: str, max_usd: float, transport=http) -> list:
    # timeout=280: the Actor stops starting requests 20 s before its own timeout, so the run
    # ends before run-sync's 300 s limit and the items it already wrote come back.
    qs = urllib.parse.urlencode({"maxTotalChargeUsd": max_usd, "timeout": 280, "clean": "true"})
    url = f"{APIFY_BASE}/acts/{APIFY_ACTOR}/run-sync-get-dataset-items?{qs}"
    items = json.loads(transport(url, body=body, timeout=320, headers={"Authorization": f"Bearer {token}"}))
    if not isinstance(items, list):
        raise RuntimeError(f"unexpected Apify response: {str(items)[:300]}")
    return items


# ---------------------------------------------------------------- dedupe and CSV

def load_seen(path: Path) -> set[str]:
    if not path.exists():
        return set()
    with path.open(newline="", encoding="utf-8") as f:
        return {row["uid"] for row in csv.DictReader(f) if row.get("uid")}


def select_new(rows: list[dict], seen: set[str], today: str) -> list[dict]:
    new, batch = [], set()
    for r in rows:
        if r["uid"] in seen or r["uid"] in batch:
            continue
        batch.add(r["uid"])
        new.append({**r, "first_seen": today})
    return new


def write_csv(path: Path, rows: list[dict], append: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = "a" if append and path.exists() and path.stat().st_size > 0 else "w"
    with path.open(mode, newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS, extrasaction="ignore")
        if mode == "w":
            w.writeheader()
        w.writerows(rows)


# ---------------------------------------------------------------- CLI

def main(argv=None, transport=http, now=None, delay=0.25) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--source", choices=["public", "apify"], default=os.environ.get("JOBS_SOURCE") or "public")
    p.add_argument("--sites-file", default="sites.txt", type=Path)
    p.add_argument("--keyword", action="append", default=[], help="keep titles containing any of these (repeatable)")
    p.add_argument("--out-dir", default="data", type=Path)
    p.add_argument("--details", action="store_true",
                   help="public only: one extra request per NEW job for its exact posting date, "
                        "all locations and hiring entity")
    p.add_argument("--max-requests", type=int, default=500,
                   help="public only: list requests per site before stopping (default 500, about 10,000 jobs)")
    p.add_argument("--max-usd", type=float, default=float(os.environ.get("APIFY_MAX_USD") or "0.50"),
                   help="apify only: hard cap on what one run can charge (default 0.50)")
    p.add_argument("--max-jobs", type=int, default=500, help="apify only: stop after this many jobs")
    p.add_argument("--dry-run", action="store_true", help="print the parsed sites or the Apify request and exit")
    a = p.parse_args(argv)

    urls, valid = read_sites(a.sites_file), []
    for u in urls:
        try:
            valid.append((u, parse_site(u)))
        except ValueError as e:
            print("skip:", e, file=sys.stderr)
    if not valid:
        print(f"No valid Workday career site URL in {a.sites_file}", file=sys.stderr)
        return 2
    now = now or datetime.now(timezone.utc)
    today = now.date()
    errors = []

    if a.source == "apify":
        body = actor_input([u for u, _ in valid], a.keyword, max_jobs=a.max_jobs)
        if a.dry_run:
            print(json.dumps({"actor": APIFY_ACTOR, "maxTotalChargeUsd": a.max_usd, "input": body}, indent=2))
            return 0
        token = os.environ.get("APIFY_TOKEN")
        if not token:
            print("Set APIFY_TOKEN (Apify Console > Settings > API & Integrations).", file=sys.stderr)
            return 2
        try:
            items = run_actor(body, token, a.max_usd, transport)
        except urllib.error.HTTPError as e:
            if e.code == 408:
                print("The run did not finish within run-sync's 300 s limit. List fewer sites or add keywords.",
                      file=sys.stderr)
            else:
                print(f"Apify API error {e.code}: {e.read()[:300]!r}", file=sys.stderr)
            return 1
        for i in items:
            if i.get("type") == "status":
                print("note:", i.get("message"), file=sys.stderr)
        rows = [from_actor_item(i) for i in items if is_job_item(i)]
    else:
        if a.dry_run:
            for u, s in valid:
                print(f"{u} -> {s.api}/jobs")
            return 0
        rows, postings, errors = fetch_public([u for u, _ in valid], a.keyword, today, transport,
                                              delay=delay, max_requests=a.max_requests)
        for e in errors:
            print("error:", e, file=sys.stderr)

    all_path, new_path = a.out_dir / "jobs.csv", a.out_dir / "new_jobs.csv"
    new = select_new(rows, load_seen(all_path), today.isoformat())
    if a.source == "public" and a.details:
        for i, r in enumerate(new):
            reader, posting = postings[r["uid"]]
            try:
                new[i] = add_details(r, reader.detail(posting))
            except (urllib.error.URLError, json.JSONDecodeError) as e:
                print(f"warning: no details for {r['url']}: {e}", file=sys.stderr)
    write_csv(all_path, new, append=True)
    write_csv(new_path, new, append=False)
    print(f"{len(rows)} jobs read, {len(new)} new, written to {new_path}")
    return 1 if errors and not rows else 0


if __name__ == "__main__":
    sys.exit(main())
