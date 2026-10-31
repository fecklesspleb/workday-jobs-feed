"""Builds workday-jobs-to-google-sheets.n8n.json. Edit here, then run: python3 build.py"""
import json
import uuid

NS = uuid.UUID("2b7d9e40-5c1a-4f63-8d2e-71a9c4e0b356")


def nid(name):
    return str(uuid.uuid5(NS, name))


ACTOR_URL = "https://api.apify.com/v2/acts/fecklesspleb~workday-jobs-scraper/run-sync-get-dataset-items"

MAIN_NOTE = """## Daily new jobs from Workday career sites to Google Sheets

### How it works
1. **Every morning at 8** starts the workflow.
2. **Search settings** holds the Workday career site URLs, title keywords and a spending cap.
3. **Run Workday Jobs Scraper** calls the Apify API (run-sync-get-dataset-items) for the fecklesspleb/workday-jobs-scraper Actor. It reads each site's public job list, splits large sites by facet to get past the 2,000-result cap, and with monitoring mode (onlyNewJobs) returns only jobs it has not returned before, so later runs return and charge only new postings.
4. **Keep job records** drops the free status and expired-job records the Actor can also write.
5. **Append or update job rows** writes one row per job, matched on Job ID.

### Setup
1. Apify: copy your API token from Apify Console > Settings > API & Integrations. Create a **Header Auth** credential named `Apify API` with Name `Authorization` and Value `Bearer YOUR_TOKEN`, and select it in **Run Workday Jobs Scraper**.
2. Google Sheets: make a sheet whose first row has these headers: Job ID, Company, Title, Location, Employment type, Posted, Salary, URL, First seen. Connect your Google Sheets credential and pick the document and tab in **Append or update job rows**.
3. Edit **Search settings**: career site URLs (copy them from the browser up to the site name), keywords, and `maxUsd`. Keep at least one URL: with an empty list the Actor runs a short paid demo instead.
4. Click **Execute workflow** once to test, then activate it.

### Cost
The Actor charges $0.002 per job returned. `maxUsd` caps each run (default $0.50).

### Customization tips
- Use a different `stateStoreName` for each independent search.
- Add a Gmail or Slack node after **Shape job row** to get the new jobs as a message."""


def sticky(name, content, x, y, w, h, color=None):
    p = {"content": content, "height": h, "width": w}
    if color is not None:
        p["color"] = color
    return {"parameters": p, "id": nid(name), "name": name, "type": "n8n-nodes-base.stickyNote",
            "typeVersion": 1, "position": [x, y]}


def assignment(name, value, typ):
    return {"id": nid("a-" + name), "name": name, "value": value, "type": typ}


nodes = [
    sticky("Main note", MAIN_NOTE, -620, -260, 520, 860),
    sticky("Section: settings", "### 1. Schedule and settings\nChange the run time, career sites, keywords and spending cap here.",
           -60, -60, 460, 360, 7),
    sticky("Section: scrape", "### 2. Run the Actor\nOne Apify API call per run. maxTotalChargeUsd stops spend at your cap.",
           420, -60, 460, 360, 7),
    sticky("Section: sheet", "### 3. Save to Google Sheets\nKeeps job records only and upserts them by Job ID.",
           900, -60, 680, 360, 7),
    {
        "parameters": {"rule": {"interval": [{"triggerAtHour": 8}]}},
        "id": nid("Every morning at 8"), "name": "Every morning at 8",
        "type": "n8n-nodes-base.scheduleTrigger", "typeVersion": 1.2, "position": [0, 100],
    },
    {
        "parameters": {
            "assignments": {"assignments": [
                assignment("companies", '["https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite", "https://workday.wd5.myworkdayjobs.com/Workday"]', "array"),
                assignment("keywords", '["engineer", "data"]', "array"),
                assignment("stateStoreName", "n8n-workday-jobs", "string"),
                assignment("maxUsd", "0.5", "number"),
            ]},
            "options": {},
        },
        "id": nid("Search settings"), "name": "Search settings",
        "type": "n8n-nodes-base.set", "typeVersion": 3.4, "position": [200, 100],
    },
    {
        "parameters": {
            "method": "POST",
            "url": ACTOR_URL,
            "authentication": "genericCredentialType",
            "genericAuthType": "httpHeaderAuth",
            "sendQuery": True,
            "queryParameters": {"parameters": [
                {"name": "maxTotalChargeUsd", "value": "={{ $json.maxUsd }}"},
                {"name": "timeout", "value": "280"},
                {"name": "clean", "value": "true"},
            ]},
            "sendBody": True,
            "specifyBody": "json",
            "jsonBody": "={{ JSON.stringify({ companies: $json.companies, keywords: $json.keywords, onlyNewJobs: true, stateStoreName: $json.stateStoreName, includeDescription: false, maxTotalJobs: 500 }) }}",
            "options": {"timeout": 320000},
        },
        "id": nid("Run Workday Jobs Scraper"), "name": "Run Workday Jobs Scraper",
        "type": "n8n-nodes-base.httpRequest", "typeVersion": 4.2, "position": [560, 100],
    },
    {
        "parameters": {
            "conditions": {
                "options": {"caseSensitive": True, "leftValue": "", "typeValidation": "loose", "version": 2},
                "conditions": [{
                    "id": nid("c-is-job"),
                    "leftValue": "={{ !$json.type && ($json.status === undefined || $json.status === null || $json.status === 'active') && !!$json.uid && !!$json.url }}",
                    "rightValue": "",
                    "operator": {"type": "boolean", "operation": "true", "singleValue": True},
                }],
                "combinator": "and",
            },
            "looseTypeValidation": True,
            "options": {},
        },
        "id": nid("Keep job records"), "name": "Keep job records",
        "type": "n8n-nodes-base.filter", "typeVersion": 2.2, "position": [960, 100],
    },
    {
        "parameters": {
            "assignments": {"assignments": [
                assignment("Job ID", "={{ $json.uid }}", "string"),
                assignment("Company", "={{ $json.companyName || $json.companySlug }}", "string"),
                assignment("Title", "={{ $json.title }}", "string"),
                assignment("Location", "={{ ($json.locations || [$json.location]).filter(Boolean).join('; ') }}", "string"),
                assignment("Employment type", "={{ $json.employmentType || '' }}", "string"),
                assignment("Posted", "={{ $json.postedAt ? String($json.postedAt).slice(0, 10) : '' }}", "string"),
                assignment("Salary", "={{ $json.salaryText || '' }}", "string"),
                assignment("URL", "={{ $json.url }}", "string"),
                assignment("First seen", "={{ $now.toISODate() }}", "string"),
            ]},
            "options": {},
        },
        "id": nid("Shape job row"), "name": "Shape job row",
        "type": "n8n-nodes-base.set", "typeVersion": 3.4, "position": [1180, 100],
    },
    {
        "parameters": {
            "operation": "appendOrUpdate",
            "documentId": {"__rl": True, "value": "", "mode": "list", "cachedResultName": ""},
            "sheetName": {"__rl": True, "value": "", "mode": "list", "cachedResultName": ""},
            "columns": {"mappingMode": "autoMapInputData", "value": {}, "matchingColumns": ["Job ID"], "schema": []},
            "options": {},
        },
        "id": nid("Append or update job rows"), "name": "Append or update job rows",
        "type": "n8n-nodes-base.googleSheets", "typeVersion": 4.7, "position": [1400, 100],
    },
]

chain = ["Every morning at 8", "Search settings", "Run Workday Jobs Scraper", "Keep job records",
         "Shape job row", "Append or update job rows"]
connections = {a: {"main": [[{"node": b, "type": "main", "index": 0}]]} for a, b in zip(chain, chain[1:])}

wf = {
    "name": "Daily new jobs from Workday career sites to Google Sheets",
    "nodes": nodes,
    "connections": connections,
    "pinData": {},
    "settings": {"executionOrder": "v1"},
    "meta": {"templateCredsSetupCompleted": False},
    "tags": [],
}

if __name__ == "__main__":
    with open("workday-jobs-to-google-sheets.n8n.json", "w", encoding="utf-8") as f:
        json.dump(wf, f, indent=2, ensure_ascii=False)
        f.write("\n")
    print("wrote workday-jobs-to-google-sheets.n8n.json,", len(nodes), "nodes")
