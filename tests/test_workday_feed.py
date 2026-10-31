import csv
import io
import json
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stderr, redirect_stdout
from datetime import date, datetime, timezone
from pathlib import Path

import workday_feed as wf
from tests.fake_workday import FakeWorkday, make_jobs

FIX = Path(__file__).parent / "fixtures"
SITE = "https://acme.wd1.myworkdayjobs.com/Site"
NOW = datetime(2026, 10, 31, 12, tzinfo=timezone.utc)


def reader(fake, cap=2000, max_requests=500):
    return wf.WorkdayReader(wf.parse_site(SITE), fake, delay=0, max_requests=max_requests, cap=cap)


def run(tmp, fake, *args, sites=(SITE,)):
    sf = Path(tmp) / "sites.txt"
    sf.write_text("# comment\n" + "\n".join(sites) + "\n")
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = wf.main(["--sites-file", str(sf), "--out-dir", str(Path(tmp) / "data"), *args],
                     transport=fake, now=NOW, delay=0)
    return rc, out.getvalue(), err.getvalue()


def read_csv(path):
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


class ParseSite(unittest.TestCase):
    def test_forms(self):
        s = wf.parse_site("https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite/details/x")
        self.assertEqual((s.host, s.tenant, s.name), ("nvidia.wd5.myworkdayjobs.com", "nvidia", "NVIDIAExternalCareerSite"))
        self.assertEqual(s.api, "https://nvidia.wd5.myworkdayjobs.com/wday/cxs/nvidia/NVIDIAExternalCareerSite")
        s = wf.parse_site("https://wd5.myworkdaysite.com/en-US/recruiting/Acme/Careers")
        self.assertEqual((s.host, s.tenant, s.name), ("acme.wd5.myworkdayjobs.com", "acme", "Careers"))
        s = wf.parse_site("salesforce.wd12.myworkdayjobs.com/External_Career_Site")
        self.assertEqual(s.key, "salesforce/External_Career_Site")
        for bad in ("https://example.org/jobs", "https://acme.wd1.myworkdayjobs.com/", "https://wd1.myworkdaysite.com/x"):
            with self.assertRaises(ValueError):
                wf.parse_site(bad)

    def test_sites_file_comments(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / "s.txt"
            p.write_text("# header\n\nhttps://a.wd1.myworkdayjobs.com/X  # note\n")
            self.assertEqual(wf.read_sites(p), ["https://a.wd1.myworkdayjobs.com/X"])


class JobId(unittest.TestCase):
    def test_variants(self):
        self.assertEqual(wf.job_id({"externalPath": "/job/A/B_JR1", "bulletFields": ["JR1"]}), "JR1")
        # path suffix after a repost
        self.assertEqual(wf.job_id({"externalPath": "/job/A/B_JR2028745-1", "bulletFields": ["JR2028745"]}), "JR2028745")
        # Intel lists a label before the ID
        self.assertEqual(wf.job_id({"externalPath": "/job/A/B_JR0286800", "bulletFields": ["Spotlight Job", "JR0286800"]}), "JR0286800")
        # IDs with a hyphen (Workday's own site)
        self.assertEqual(wf.job_id({"externalPath": "/job/A/B_JR-0109720-1", "bulletFields": ["JR-0109720"]}), "JR-0109720")
        self.assertEqual(wf.job_id({"externalPath": "/job/A/B_R123", "bulletFields": []}), "R123")
        self.assertIsNone(wf.job_id({"title": "no page", "bulletFields": ["JR9"]}))


class Reading(unittest.TestCase):
    def ids(self, r):
        return [jid for jid, _ in r.jobs()]

    def test_small_site_pages_through(self):
        fake = FakeWorkday(make_jobs(45))
        r = reader(fake)
        self.assertEqual(len(self.ids(r)), 45)
        self.assertEqual(r.requests, 3)
        self.assertEqual(r.partial, [])
        self.assertTrue(all(c[1]["limit"] == 20 and c[1]["appliedFacets"] == {} for c in fake.list_calls()))

    def test_cap_needs_split(self):
        # 250 jobs, cap 100: one query sees only 100; four families of ~63 each fit.
        fake = FakeWorkday(make_jobs(250))
        r = reader(fake, cap=100)
        ids = self.ids(r)
        self.assertEqual(len(ids), 250)
        self.assertEqual(len(set(ids)), 250)
        self.assertEqual(r.partial, [])
        used = {k for c in fake.list_calls() for k in c[1]["appliedFacets"]}
        self.assertEqual(used, {"family"})  # never the distance facet or the countless one

    def test_two_facets_when_one_is_not_enough(self):
        # 2 families of 150 with cap 100: each family must be split again by region.
        fake = FakeWorkday(make_jobs(300, families=2, regions=3))
        r = reader(fake, cap=100)
        self.assertEqual(sorted(self.ids(r)), sorted(j["id"] for j in fake.jobs))
        self.assertEqual(r.partial, [])
        self.assertTrue(any(len(c[1]["appliedFacets"]) == 2 for c in fake.list_calls()))

    def test_no_usable_facet_is_partial(self):
        fake = FakeWorkday(make_jobs(250), facets=())  # only distance and locationMainGroup
        r = reader(fake, cap=100)
        self.assertEqual(len(self.ids(r)), 100)
        self.assertEqual(len(r.partial), 1)
        self.assertIn("no facet splits the site", r.partial[0])

    def test_request_budget(self):
        fake = FakeWorkday(make_jobs(200))
        r = reader(fake, max_requests=4)
        self.assertEqual(len(self.ids(r)), 80)
        self.assertIn("--max-requests", r.partial[0])

    def test_pathless_and_duplicates(self):
        extra = [{"id": "X1", "title": "Ghost", "family": "F0", "region": "R0", "path": None, "bullets": ["X1"]},
                 {"id": "JR100001", "title": "Repost", "family": "F1", "region": "R1",
                  "path": "/job/City/Repost_JR100001-1", "bullets": ["JR100001"]}]
        r = reader(FakeWorkday(make_jobs(10, extra=extra)))
        ids = self.ids(r)
        self.assertEqual(len(ids), 10)
        self.assertEqual(r.pathless, 1)

    def test_approx_posted(self):
        d = date(2026, 10, 31)
        self.assertEqual(wf.approx_posted("Posted Today", d), "2026-10-31")
        self.assertEqual(wf.approx_posted("Posted Yesterday", d), "2026-10-30")
        self.assertEqual(wf.approx_posted("Posted 3 Days Ago", d), "2026-10-28")
        self.assertIsNone(wf.approx_posted("Posted 30+ Days Ago", d))
        self.assertIsNone(wf.approx_posted(None, d))


class Robots(unittest.TestCase):
    def test_disallowed_site_is_skipped(self):
        fake = FakeWorkday(make_jobs(5), robots="User-agent: *\nDisallow: /Site/\n")
        with tempfile.TemporaryDirectory() as t:
            rc, out, err = run(t, fake)
        self.assertEqual(rc, 1)
        self.assertIn("robots.txt disallows /Site/", err)
        self.assertEqual(fake.list_calls(), [])
        self.assertEqual(fake.calls[0][2]["accept"], "text/plain")

    def test_missing_robots_allows(self):
        fake = FakeWorkday(make_jobs(5), robots=None)
        with tempfile.TemporaryDirectory() as t:
            rc, out, err = run(t, fake)
        self.assertEqual(rc, 0)
        self.assertIn("5 new", out)


class PublicEndToEnd(unittest.TestCase):
    def test_dedupe_across_runs_and_keywords(self):
        fake = FakeWorkday(make_jobs(30))
        with tempfile.TemporaryDirectory() as t:
            rc, out, _ = run(t, fake, "--keyword", "engineer")
            self.assertEqual(rc, 0)
            rows = read_csv(Path(t) / "data/new_jobs.csv")
            self.assertEqual(len(rows), 15)
            self.assertEqual(list(rows[0]), wf.COLUMNS)
            self.assertEqual(rows[0]["uid"], "workday:acme:JR100001")
            self.assertEqual(rows[0]["url"], "https://acme.wd1.myworkdayjobs.com/Site/job/City/Role-1_JR100001")
            self.assertEqual(rows[0]["posted"], "2026-10-31")
            self.assertEqual(rows[0]["first_seen"], "2026-10-31")

            run(t, fake, "--keyword", "engineer")
            self.assertEqual(read_csv(Path(t) / "data/new_jobs.csv"), [])

            fake.jobs.append({"id": "JR999", "title": "Data Engineer", "family": "F0", "region": "R0",
                              "path": "/job/City/Data_JR999", "bullets": ["JR999"]})
            run(t, fake, "--keyword", "engineer")
            self.assertEqual([r["job_id"] for r in read_csv(Path(t) / "data/new_jobs.csv")], ["JR999"])
            self.assertEqual(len(read_csv(Path(t) / "data/jobs.csv")), 16)

    def test_details_only_for_new_jobs(self):
        fake = FakeWorkday(make_jobs(4))
        with tempfile.TemporaryDirectory() as t:
            run(t, fake, "--details")
            rows = read_csv(Path(t) / "data/new_jobs.csv")
            self.assertEqual(rows[0]["posted"], "2026-10-01")
            self.assertEqual(rows[0]["location"], "City; Town")
            self.assertEqual(rows[0]["company_name"], "Example Corp")
            n = len(fake.calls)
            run(t, fake, "--details")
            detail_calls = [c for c in fake.calls[n:] if "/job/" in c[0]]
            self.assertEqual(detail_calls, [])

    def test_no_valid_sites(self):
        fake = FakeWorkday([])
        with tempfile.TemporaryDirectory() as t:
            rc, _, err = run(t, fake, sites=("https://example.org/careers",))
        self.assertEqual(rc, 2)
        self.assertEqual(fake.calls, [])


class Apify(unittest.TestCase):
    ITEMS = [
        {"type": "status", "message": "1 site could not be read"},
        {"id": "JR1", "uid": "workday:acme:JR1", "companySlug": "acme/Site", "companyName": "Acme US",
         "title": "Engineer", "location": "City", "postedAt": "2026-10-30",
         "url": "https://acme.wd1.myworkdayjobs.com/Site/job/x_JR1", "status": "active"},
        {"id": "JR2", "uid": "workday:acme:JR2", "companySlug": "acme/Site", "companyName": "Acme US",
         "title": "Old", "url": "https://acme.wd1.myworkdayjobs.com/Site/job/x_JR2", "status": "expired"},
    ]

    def test_request_shape_and_mapping(self):
        fake = FakeWorkday([])
        fake.apify_response = self.ITEMS
        schema = json.loads((FIX / "actor_input_schema.json").read_text())
        with tempfile.TemporaryDirectory() as t:
            import os
            os.environ["APIFY_TOKEN"] = "tkn"
            try:
                rc, out, err = run(t, fake, "--source", "apify", "--keyword", "engineer", "--max-usd", "0.25")
            finally:
                del os.environ["APIFY_TOKEN"]
            self.assertEqual(rc, 0)
            url, body, kw = fake.calls[0]
            self.assertTrue(url.startswith(
                "https://api.apify.com/v2/acts/fecklesspleb~workday-jobs-scraper/run-sync-get-dataset-items?"))
            self.assertIn("maxTotalChargeUsd=0.25", url)
            self.assertIn("timeout=280", url)
            self.assertNotIn("tkn", url)
            self.assertEqual(kw["headers"], {"Authorization": "Bearer tkn"})
            self.assertGreater(kw["timeout"], 300)
            self.assertTrue(set(body) <= set(schema["properties"]), set(body) - set(schema["properties"]))
            self.assertEqual(body["companies"], [SITE])
            self.assertTrue(body["onlyNewJobs"])
            self.assertFalse(body["includeDescription"])
            rows = read_csv(Path(t) / "data/new_jobs.csv")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["company_name"], "Acme US")  # from companyName
            self.assertEqual(rows[0]["uid"], "workday:acme:JR1")
            self.assertEqual(rows[0]["posted"], "2026-10-30")
            self.assertIn("1 site could not be read", err)

    def test_no_token(self):
        fake = FakeWorkday([])
        with tempfile.TemporaryDirectory() as t:
            rc, _, err = run(t, fake, "--source", "apify")
        self.assertEqual(rc, 2)
        self.assertEqual(fake.calls, [])

    def test_never_sends_empty_companies(self):
        # An empty companies list starts the Actor's paid demo, so the script must not call it.
        fake = FakeWorkday([])
        import os
        os.environ["APIFY_TOKEN"] = "tkn"
        try:
            with tempfile.TemporaryDirectory() as t:
                rc, _, _ = run(t, fake, "--source", "apify", sites=("not a url",))
        finally:
            del os.environ["APIFY_TOKEN"]
        self.assertEqual(rc, 2)
        self.assertEqual(fake.calls, [])

    def test_408(self):
        fake = FakeWorkday([])
        fake.apify_response = urllib.error.HTTPError("u", 408, "Timeout", {}, io.BytesIO(b""))
        import os
        os.environ["APIFY_TOKEN"] = "tkn"
        try:
            with tempfile.TemporaryDirectory() as t:
                rc, _, err = run(t, fake, "--source", "apify")
        finally:
            del os.environ["APIFY_TOKEN"]
        self.assertEqual(rc, 1)
        self.assertIn("300 s", err)


if __name__ == "__main__":
    unittest.main()
