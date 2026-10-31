"""Offline test copy of the template: schedule -> manual trigger, Apify URL -> local mock server,
credential -> plain header, Google Sheets -> NoOp. Usage: make_test_variant.py OUT.json PORT"""
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
wf = json.loads((HERE.parent / "workday-jobs-to-google-sheets.n8n.json").read_text())
wf["name"] = "TEST " + wf["name"]
wf["id"] = "TestWorkdayJobs01"
for n in wf["nodes"]:
    t, p = n["type"], n["parameters"]
    if t.endswith("scheduleTrigger"):
        n.update(type="n8n-nodes-base.manualTrigger", typeVersion=1, parameters={})
    elif t.endswith("httpRequest"):
        p["url"] = p["url"].replace("https://api.apify.com", "http://127.0.0.1:" + sys.argv[2])
        p["authentication"] = "none"
        p.pop("genericAuthType")
        p["sendHeaders"] = True
        p["headerParameters"] = {"parameters": [{"name": "Authorization", "value": "Bearer test-token"}]}
    elif t.endswith("googleSheets"):
        n.update(type="n8n-nodes-base.noOp", typeVersion=1, parameters={})
Path(sys.argv[1]).write_text(json.dumps(wf, indent=1))
