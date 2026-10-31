"""An in-memory stand-in for a Workday CXS endpoint, with the behaviour observed on live tenants:
pages of at most 20, `total` only on the first page and capped, offsets past the cap returning
the first page again, facet counts computed for the current filter, a distance facet whose
buckets each count 1, and a facet with no counts at all."""
import json
import urllib.error


def make_jobs(n, families=4, regions=3, extra=()):
    jobs = []
    for i in range(n):
        jid = f"JR{100000 + i}"
        jobs.append({"id": jid, "title": f"{'Software Engineer' if i % 2 else 'Accountant'} {i}",
                     "family": f"F{i % families}", "region": f"R{i % regions}",
                     "path": f"/job/City/Role-{i}_{jid}", "bullets": [jid]})
    jobs.extend(extra)
    return jobs


class FakeWorkday:
    def __init__(self, jobs, cap=2000, robots="User-agent: *\nAllow: /Site/\n", facets=("family", "region")):
        self.jobs, self.cap, self.robots, self.facet_names = jobs, cap, robots, facets
        self.calls = []  # (url, body, kwargs)
        self.apify_response = []

    def _match(self, job, applied):
        return all(f"{k}:{job.get(k)}" in v for k, v in applied.items())

    def _facets(self, jobs):
        out = []
        for name in self.facet_names:
            counts = {}
            for j in jobs:
                counts[j.get(name)] = counts.get(j.get(name), 0) + 1
            out.append({"facetParameter": name, "values": [
                {"descriptor": k, "id": f"{name}:{k}", "count": c} for k, c in sorted(counts.items())]})
        out.append({"facetParameter": "distance", "values": [
            {"descriptor": f"{d} mi", "id": f"distance:{d}", "count": 1} for d in (5, 10, 25)]})
        out.append({"facetParameter": "locationMainGroup", "values": [
            {"facetParameter": "locations", "descriptor": "Locations", "values": []}]})
        return out

    def __call__(self, url, body=None, accept="application/json", timeout=30, headers=None):
        self.calls.append((url, body, {"accept": accept, "timeout": timeout, "headers": headers}))
        if url.endswith("/robots.txt"):
            if self.robots is None:
                raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
            return self.robots.encode()
        if "api.apify.com" in url:
            if isinstance(self.apify_response, Exception):
                raise self.apify_response
            return json.dumps(self.apify_response).encode()
        if url.endswith("/jobs"):
            applied = body["appliedFacets"]
            assert body["limit"] <= 20, "limit above 20 returns 400"
            hits = [j for j in self.jobs if self._match(j, applied)]
            offset = body["offset"] if body["offset"] < self.cap else 0
            page = hits[offset:offset + body["limit"]]
            postings = []
            for j in page:
                p = {"title": j["title"], "locationsText": "City", "postedOn": "Posted Today",
                     "bulletFields": j["bullets"]}
                if j["path"]:
                    p["externalPath"] = j["path"]
                postings.append(p)
            resp = {"jobPostings": postings, "total": min(len(hits), self.cap) if offset == 0 else 0}
            if offset == 0:
                resp["facets"] = self._facets(hits)
            return json.dumps(resp).encode()
        for j in self.jobs:
            if j["path"] and url.endswith(j["path"]):
                return json.dumps({"jobPostingInfo": {"startDate": "2026-10-01", "location": "City",
                                                      "additionalLocations": ["Town"], "jobReqId": j["id"]},
                                   "hiringOrganization": {"name": "2100 Example Corp"}}).encode()
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)

    def list_calls(self):
        return [c for c in self.calls if c[0].endswith("/jobs")]
