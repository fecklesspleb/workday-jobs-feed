"""Live checks against public Workday career sites. Skipped unless LIVE=1. No Apify calls."""
import os
import unittest

import workday_feed as wf


@unittest.skipUnless(os.environ.get("LIVE") == "1", "set LIVE=1 to run against real Workday sites")
class Live(unittest.TestCase):
    def test_forced_split_matches_plain_paging(self):
        site = wf.parse_site("https://workday.wd5.myworkdayjobs.com/Workday")
        self.assertTrue(wf.site_allowed(site))
        plain = wf.WorkdayReader(site, delay=0.25)
        plain_ids = {j for j, _ in plain.jobs()}
        self.assertGreater(len(plain_ids), 100)
        self.assertEqual(plain.partial, [])
        # Pretend the cap is 150 so the facet split has to run on a site small enough to check fully.
        split = wf.WorkdayReader(site, delay=0.25, cap=150)
        split_ids = {j for j, _ in split.jobs()}
        self.assertEqual(split.partial, [])
        missing, extra = plain_ids - split_ids, split_ids - plain_ids
        # Postings can change between the two reads; allow a little drift, not a gap.
        self.assertLessEqual(len(missing) + len(extra), 3, (missing, extra))

    def test_site_over_the_real_cap(self):
        site = wf.parse_site("https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite")
        first = wf.WorkdayReader(site, delay=0).search({})
        if first["total"] < wf.CAP:
            self.skipTest("NVIDIA is under the cap today")
        r = wf.WorkdayReader(site, delay=0.25)
        ids = {j for j, _ in r.jobs()}
        self.assertEqual(r.partial, [])
        self.assertGreater(len(ids), wf.CAP)


if __name__ == "__main__":
    unittest.main()
