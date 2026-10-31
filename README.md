# workday-jobs-feed

A small Python script and a GitHub Actions schedule that check Workday career sites once a day and write **only the jobs you have not seen before** to CSV.

- `data/new_jobs.csv`: jobs first seen on the latest run (overwritten each run).
- `data/jobs.csv`: every job ever seen, one row per job (appended).

It reads the same JSON endpoint the career page itself calls, gets past the 2,000-result ceiling of a single query by splitting the site with its own facets, and deduplicates by job ID. How that endpoint works, and why the facets are needed, is written up in [Reading Workday job postings as JSON and getting past the 2,000-result ceiling](https://dev.to/fecklesspleb/reading-workday-job-postings-as-json-and-getting-past-the-2000-result-ceiling-2cco).

## Run it locally

Python 3.9 or newer, no packages to install.

```bash
git clone https://github.com/fecklesspleb/workday-jobs-feed
cd workday-jobs-feed
python3 workday_feed.py --keyword engineer
python3 workday_feed.py --keyword engineer   # second run: 0 new, unless something was posted in between
```

On October 31, 2026 the three sample sites listed 4,613 jobs: Workday 375 (19 requests), Salesforce 1,554 (78 requests) and NVIDIA 2,684 (144 requests; NVIDIA is over the cap, so it was read in 15 job-family slices). 2,055 of them had "engineer" in the title. The run took about 4 minutes 20 seconds with a quarter-second pause between requests.

Edit `sites.txt` to choose sites, one URL per line, copied from the browser up to the site name. Both URL forms work:

```
https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite
https://wd5.myworkdaysite.com/en-US/recruiting/<tenant>/<site>
```

Options:

```
--source public|apify     default public (or env JOBS_SOURCE)
--keyword WORD            keep titles containing WORD, case-insensitive; repeatable
--sites-file PATH         default sites.txt
--out-dir PATH            default data
--details                 public only: one extra request per NEW job for its exact posting date,
                          all of its locations and the hiring entity
--max-requests 500        public only: list requests per site before it stops (500 pages is 10,000 jobs)
--max-usd 0.50            apify only: hard cap on what one run can charge (or env APIFY_MAX_USD)
--max-jobs 500            apify only: stop after this many jobs
--dry-run                 print the parsed sites (public) or the Actor request (apify) and exit, no network
```

## What it does with each site

1. Checks the tenant's `robots.txt` for the site path (`/NVIDIAExternalCareerSite/`) and skips the site if it is disallowed.
2. Asks for the first page of 20. If `total` is under 2,000 it pages through with `offset`.
3. If the site is at the cap, it picks a facet from that first response whose buckets all have counts, add up to at least the total, and have the smallest largest bucket, then reads each bucket on its own. A bucket that is still at the cap is split again by another facet (up to three facets deep). The `distance` facet, whose buckets each claim one job, fails the "add up to the total" test, so it is never used.
4. Takes the job ID from `bulletFields` when one entry matches the end of the job's URL path, otherwise from the path itself. That covers sites that put a label such as "Spotlight Job" first in `bulletFields`, and reposted jobs whose path ends in `-1`.
5. Drops repeats (a job can show up in more than one bucket) and list entries with no job page.

Each job's key is `uid = workday:<tenant>:<job id>`, for example `workday:nvidia:JR2001996`. A job is new when its `uid` is not in `data/jobs.csv`.

Columns:

`uid, site, job_id, title, location, posted, company_name, url, first_seen`

- `location` is Workday's list text, which says "2 Locations" for multi-location jobs. With `--details` it is the full list.
- `posted` comes from labels such as "Posted 3 Days Ago", counted back from the run's UTC date, so it can be a day off. "Posted 30+ Days Ago" leaves it blank. With `--details` it is the posting's start date.
- `company_name` is filled only with `--details` (the hiring entity, with a leading company code such as `2100` removed).

If a site cannot be split under the cap, or a site hits `--max-requests`, the script still writes what it read and prints a warning naming the site. Jobs it missed are picked up on a later run if they become reachable.

## Run it every day with GitHub Actions

`.github/workflows/workday-jobs.yml` runs at 11:17 UTC every day (and on demand from the Actions tab). It runs the offline tests, runs `workday_feed.py`, and commits `data/jobs.csv` and `data/new_jobs.csv` when they change.

1. Fork or copy this repository.
2. Edit `sites.txt`.
3. Optional, under Settings > Secrets and variables > Actions:
   - Variable `KEYWORDS`, comma separated, for example `engineer, data scientist`.
   - To use the Actor: variable `JOBS_SOURCE` = `apify`, secret `APIFY_TOKEN`, and optionally variable `APIFY_MAX_USD`.
4. Open the Actions tab, enable workflows if asked, and run **Workday jobs feed** once by hand.

GitHub disables scheduled workflows in public repositories after 60 days with no repository activity, and scheduled workflows are off by default in a fork. The workflow commits only on days with new jobs, so a quiet watch list can switch itself off; check the Actions tab now and then.

## The Apify option

`--source apify` sends the same site list to the [Workday Jobs Scraper](https://apify.com/fecklesspleb/workday-jobs-scraper) Actor on Apify in one `run-sync-get-dataset-items` call. Disclosure: I built and sell usage of that Actor. It reads every job's detail page, so each row comes with the exact posting date, the full location list, the hiring entity, employment type and any pay range written in the description, and it keeps its own "already returned" state on Apify (`onlyNewJobs`), so a daily run is charged only for jobs it has not returned before. Its `uid` has the same format as the script's, so switching sources does not report old jobs again.

```bash
export APIFY_TOKEN=...   # Apify Console > Settings > API & Integrations
python3 workday_feed.py --source apify --keyword engineer --dry-run   # check the request first
python3 workday_feed.py --source apify --keyword engineer
```

The token is sent in the `Authorization` header, never in the URL. Each run is capped with `maxTotalChargeUsd`. The script refuses to call the Actor with an empty site list, because the Actor treats an empty list as a request for a short sample run of NVIDIA jobs, which is charged.

## What a run costs

`public` source: nothing. GitHub Actions minutes are free for public repositories on standard runners.

`apify` source: the Actor's price is $0.002 per job returned (pay per event, event `job`). There is no separate start fee in its pricing, and status and expired-job records are free.

| Run | Jobs returned | Charge |
|---|---|---|
| First run, the three sample sites, keyword `engineer` | 2,055 matching titles on October 31, 2026; stops at the $0.50 default cap | $0.50 (250 jobs) |
| A later daily run with 10 new matching jobs | 10 | $0.02 |

Jobs a run leaves out because it hit the cap are not marked as returned, so the following runs pick them up. To take more of the backlog per run, raise `APIFY_MAX_USD` and `--max-jobs` (default 500, which also caps each run; the workflow uses the default), or add keywords. All 2,055 jobs would be $4.11.

## n8n version

`n8n/` holds the Apify option as an n8n workflow: a schedule, one HTTP Request to the Actor, a filter, and an upsert into Google Sheets keyed on Job ID. See `n8n/README.md`.

## Tests

```bash
python3 -m unittest -v                          # offline
LIVE=1 python3 -m unittest tests.test_live -v   # reads Workday's and NVIDIA's public career sites
```

The offline tests use an in-memory stand-in for the Workday endpoint that behaves like the real one: pages of 20, `total` only on the first page and capped, offsets past the cap returning the first page again, a `distance` facet and a facet with no counts. They check paging, the one- and two-facet splits, the partial-read warning, the request budget, job ID cases, dedupe across runs, `--details`, robots.txt handling, the exact Apify request (URL, cost cap, header, body fields against the Actor's input schema), that `companyName` lands in `company_name`, that status and expired records are dropped, the empty-list refusal and the 408 timeout path. They never touch the network or Apify.

The live tests read Workday's own career site twice, once normally and once with the cap set to 150 so the facet split has to run, and check that both reads find the same jobs; then they read NVIDIA's site past the real 2,000 cap.

## Limits

- Only sites you list are checked. This is not a search engine across companies.
- A tenant may decide to block this endpoint or change it; it is not a documented public API.
- The Apify sync endpoint waits at most 300 seconds. If a run takes longer you get HTTP 408 (the run keeps going on Apify); list fewer sites or add keywords.
- Some employers' own site terms say more than robots.txt does. Read them for the companies you track.

## License

MIT
