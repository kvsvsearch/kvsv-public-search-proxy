import os
from flask import Flask, request, jsonify
import requests

app = Flask(__name__)

# Allow the public GitHub Pages site to call this API from a real browser.
# This is a read-only, no-auth, no-cookie public search proxy -- there is
# no session/credential data to protect via CORS, only the shared NARA key
# which stays server-side regardless. Restricted to the known site origins
# rather than "*" so random third parties can't quietly burn the shared
# NARA/WikiTree rate-limit quota through this proxy.
ALLOWED_ORIGINS = {
    "https://kvsvsearch.github.io",
}

@app.after_request
def add_cors_headers(response):
    origin = request.headers.get("Origin", "")
    if origin in ALLOWED_ORIGINS:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Vary"] = "Origin"
        response.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return response

WIKITREE_URL = "https://api.wikitree.com/api.php"
NARA_URL = "https://catalog.archives.gov/api/v2/records/search"
# Read from environment only -- never hardcode a real key in source that could
# end up in a public repo or client-facing output.
NARA_API_KEY = os.environ.get("NARA_API_KEY", "")

# Jason's explicit scope: only Americas, Europe, and Africa. Exclude Australia/Oceania,
# Asia, and the Middle East. Unknown/blank locations are kept (benefit of the doubt)
# rather than dropped, since WikiTree location strings are freeform text.
EXCLUDE_KEYWORDS = [
    "australia", "new zealand", "oceania", "papua new guinea", "fiji",
    "china", "japan", "india", "korea", "thailand", "vietnam", "philippines",
    "indonesia", "malaysia", "singapore", "pakistan", "bangladesh", "sri lanka",
    "nepal", "mongolia", "taiwan", "hong kong", "cambodia", "laos", "myanmar",
    "israel", "saudi arabia", "iran", "iraq", "turkey", "syria", "lebanon",
    "jordan", "kuwait", "qatar", "united arab emirates", "yemen", "oman",
    "afghanistan", "kazakhstan", "uzbekistan", "russia", "siberia",
]

def region_allowed(*location_strings):
    combined = " ".join([s for s in location_strings if s]).lower()
    if not combined.strip():
        return True  # unknown location -- keep, don't assume it's out of scope
    return not any(bad in combined for bad in EXCLUDE_KEYWORDS)

@app.route("/api/wikitree-search")
def wikitree_search():
    last_name = request.args.get("lastName", "").strip()
    first_name = request.args.get("firstName", "").strip()
    if not last_name and not first_name:
        return jsonify({"error": "lastName or firstName required"}), 400
    params = {"action": "searchPerson", "limit": "20", "fields": "Id,Name,FirstName,MiddleName,LastNameAtBirth,LastNameCurrent,BirthDate,DeathDate,BirthLocation,DeathLocation,Gender"}
    if last_name:
        params["LastName"] = last_name
    if first_name:
        params["FirstName"] = first_name
    try:
        resp = requests.get(WIKITREE_URL, params=params, timeout=10)
        data = resp.json()
    except Exception as e:
        return jsonify({"error": str(e)}), 502
    matches = []
    if isinstance(data, list) and data and "matches" in data[0]:
        matches = data[0]["matches"]
    results = []
    excluded_count = 0
    for m in matches:
        birth_loc = m.get("BirthLocation")
        death_loc = m.get("DeathLocation")
        if not region_allowed(birth_loc, death_loc):
            excluded_count += 1
            continue
        display_name = (m.get("LongName") or f"{m.get('FirstName','')} {m.get('LastNameCurrent','')}".strip()).strip()
        if not display_name or not m.get("Name"):
            # privacy-restricted / empty WikiTree stub -- nothing useful to show
            excluded_count += 1
            continue
        results.append({
            "source": "WikiTree",
            "sourceUrl": f"https://www.wikitree.com/wiki/{m.get('Name','')}",
            "name": display_name,
            "birthDate": m.get("BirthDate"),
            "deathDate": m.get("DeathDate"),
            "birthLocation": birth_loc,
            "deathLocation": death_loc,
        })
    return jsonify({"query": {"lastName": last_name, "firstName": first_name}, "count": len(results), "excludedOutOfScope": excluded_count, "results": results})

@app.route("/api/nara-search")
def nara_search():
    """National Archives (NARA) Catalog API search -- covers federal records
    including Record Group 75 (Bureau of Indian Affairs), which holds the
    Dawes Rolls / Final Rolls enrollment cards for the Five Civilized Tribes.
    Same source, so one integration covers both general NARA records and
    tribal enrollment/Dawes Rolls searches."""
    query = request.args.get("q", "").strip()
    if not query:
        return jsonify({"error": "q required"}), 400
    if not NARA_API_KEY:
        return jsonify({"error": "NARA_API_KEY not configured on server"}), 500
    try:
        resp = requests.get(
            NARA_URL,
            params={"q": query, "limit": "20"},
            headers={"Content-Type": "application/json", "x-api-key": NARA_API_KEY},
            timeout=15,
        )
        data = resp.json()
    except Exception as e:
        return jsonify({"error": str(e)}), 502
    hits = (data.get("body", {}) or {}).get("hits", {}).get("hits", [])
    results = []
    for h in hits:
        rec = (h.get("_source", {}) or {}).get("record", {}) or {}
        na_id = h.get("_id")
        title = rec.get("title") or "Untitled record"
        start = (rec.get("inclusiveStartDate") or {}).get("year")
        end = (rec.get("inclusiveEndDate") or {}).get("year")
        notes = rec.get("generalNotes") or []
        results.append({
            "source": "National Archives (NARA)",
            "sourceUrl": f"https://catalog.archives.gov/id/{na_id}" if na_id else "https://catalog.archives.gov/",
            "name": title,
            "birthDate": str(start) if start else None,
            "deathDate": str(end) if end else None,
            "birthLocation": None,
            "deathLocation": None,
            "note": (notes[0] if notes else None),
        })
    total = ((data.get("body", {}) or {}).get("hits", {}) or {}).get("total", {}).get("value", len(results))
    return jsonify({"query": {"q": query}, "count": len(results), "totalAvailable": total, "results": results})

@app.route("/api/health")
def health():
    return jsonify({"ok": True})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8781))
    app.run(host="0.0.0.0", port=port)

# ==========================================================================
# Hub live-status feed -- added by SETUP_HUB_FEED.ps1
# ==========================================================================
import json
import time

HUB_FEED_TOKEN = os.environ.get("HUB_FEED_TOKEN", "")  # set in Railway dashboard only, never in source
HUB_FEED_STORE_PATH = os.environ.get("HUB_FEED_STORE_PATH", "/tmp/hub_feed_store.json")
_hub_feed_store = {"data": None, "updated_at": None}

if os.path.exists(HUB_FEED_STORE_PATH):
    try:
        with open(HUB_FEED_STORE_PATH) as f:
            _hub_feed_store = json.load(f)
    except Exception:
        pass

EXCLUDED_TAB_NAME = "Genealogy_RESTRICTED"
ALLOWED_TABS = {"Dashboard", "Coding_Tech_Tasks", "Status_Check_Needed", "Errors_Corrections", "Status_Log"}


@app.route("/api/hub-status", methods=["GET", "POST"])
def hub_status():
    if request.method == "POST":
        auth = request.headers.get("Authorization", "")
        if not HUB_FEED_TOKEN or auth != f"Bearer {HUB_FEED_TOKEN}":
            return jsonify({"error": "unauthorized"}), 401
        payload = request.get_json(silent=True)
        if payload is None:
            return jsonify({"error": "valid JSON body required"}), 400
        if isinstance(payload, dict):
            keys = set(payload.keys())
            if EXCLUDED_TAB_NAME in keys:
                return jsonify({"error": f"payload contains excluded tab '{EXCLUDED_TAB_NAME}' — rejected"}), 400
            unexpected = keys - ALLOWED_TABS
            if unexpected:
                return jsonify({"error": f"payload contains tabs outside the allowlist: {sorted(unexpected)} — rejected"}), 400
        _hub_feed_store["data"] = payload
        _hub_feed_store["updated_at"] = time.time()
        try:
            with open(HUB_FEED_STORE_PATH, "w") as f:
                json.dump(_hub_feed_store, f)
        except Exception as e:
            return jsonify({"error": f"stored in memory but failed to persist to disk: {e}"}), 500
        return jsonify({"ok": True, "updated_at": _hub_feed_store["updated_at"]})

    if _hub_feed_store["data"] is None:
        return jsonify({"error": "no data published yet"}), 404
    return jsonify({"data": _hub_feed_store["data"], "updated_at": _hub_feed_store["updated_at"]})
