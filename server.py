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
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
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
    # WikiTree returns HTTP 200 with a body like [{"status": "Limit exceeded."}]
    # when it is rate-limiting us -- that has no "matches" key, so it was
    # previously silently treated as "zero legitimate results" instead of an
    # error. Surface it honestly so the frontend shows a real error message
    # instead of quietly dropping every non-US/Canada result.
    if not resp.ok:
        return jsonify({"error": f"WikiTree returned HTTP {resp.status_code}"}), 502
    if isinstance(data, list) and data and isinstance(data[0], dict) and "status" in data[0] and "matches" not in data[0]:
        return jsonify({"error": f"WikiTree: {data[0]['status']}"}), 502
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

# ==========================================================================
# Contribute intake -- hardened v4
# ==========================================================================
import hashlib
import logging
import secrets
import datetime as _dt
import uuid as _uuid

_log = logging.getLogger("kvsv.contribute")

try:
    from sendgrid import SendGridAPIClient
    from sendgrid.helpers.mail import Mail
    _SENDGRID_AVAILABLE = True
except ImportError:
    _SENDGRID_AVAILABLE = False

try:
    import firebase_admin
    from firebase_admin import credentials as _fb_credentials
    from firebase_admin import firestore as _fb_firestore
    _FIREBASE_AVAILABLE = True
except ImportError:
    _FIREBASE_AVAILABLE = False

SENDGRID_API_KEY = os.environ.get("SENDGRID_API_KEY", "")
SENDGRID_FROM = os.environ.get("SENDGRID_FROM", "")
SENDGRID_TO = os.environ.get("SENDGRID_TO", "")
TURNSTILE_SECRET = os.environ.get("TURNSTILE_SECRET", "")
CONTRIBUTE_IP_PEPPER = os.environ.get("CONTRIBUTE_IP_PEPPER", "")
CONTRIBUTE_EMAIL_PEPPER = os.environ.get("CONTRIBUTE_EMAIL_PEPPER", "")
FIREBASE_CREDENTIALS_JSON = os.environ.get("FIREBASE_CREDENTIALS_JSON", "")
CONTRIBUTE_HASH_PEPPER = os.environ.get("CONTRIBUTE_HASH_PEPPER", "")  # noqa: F841

CORRECTION_CONTACT = "jasoneugeneb9@gmail.com"
MAX_BODY_BYTES = 64 * 1024
IDEMPOTENCY_WINDOW_SECONDS = 24 * 3600

RATE_LIMITS = (
    ("ip",    "minute", 60,   5),
    ("ip",    "hour",   3600, 20),
    ("email", "hour",   3600, 3),
)

CATEGORIES = {
    "Family research request",
    "Public genealogy submission",
    "Professional genealogist submission",
    "New client inquiry",
    "Correction or evidence update",
    "Oral-history submission",
    "Institutional or community archive contribution",
    "General question or referral",
}

FIELD_MAX = {
    "name": 200, "email": 320, "phone": 60, "organization": 200,
    "category": 100, "family": 200, "names": 500,
    "relationships": 500, "locations": 500, "dates": 200,
    "narrative": 8000, "research_question": 2000,
    "source_description": 2000, "document_reference": 500,
}
STRING_FIELDS = set(FIELD_MAX.keys())
BOOL_FIELDS = {"contactConsent", "publicationConsent",
               "ackPublicRecord", "ackAccuracy"}
META_FIELDS = {"idempotency_key", "turnstile_token"}
ALLOWED_FIELDS = STRING_FIELDS | BOOL_FIELDS | META_FIELDS
REQUIRED_STRINGS = ("name", "email", "category", "narrative")
REQUIRED_BOOLS = ("ackPublicRecord", "ackAccuracy")

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_contribute_firestore = None


def _get_contribute_firestore():
    global _contribute_firestore
    if _contribute_firestore is not None:
        return _contribute_firestore
    if not (_FIREBASE_AVAILABLE and FIREBASE_CREDENTIALS_JSON):
        return None
    try:
        if not firebase_admin._apps:
            cred = _fb_credentials.Certificate(json.loads(FIREBASE_CREDENTIALS_JSON))
            firebase_admin.initialize_app(cred)
        _contribute_firestore = _fb_firestore.client()
        return _contribute_firestore
    except Exception:
        _log.exception("contribute: firebase init failed")
        return None


def _verify_turnstile(token, remote_ip):
    if not TURNSTILE_SECRET:
        return False
    if not isinstance(token, str) or not token:
        return False
    try:
        r = requests.post(
            "https://challenges.cloudflare.com/turnstile/v0/siteverify",
            data={"secret": TURNSTILE_SECRET,
                  "response": token,
                  "remoteip": remote_ip or ""},
            timeout=10,
        )
        return bool(r.json().get("success"))
    except Exception:
        _log.exception("contribute: turnstile request failed")
        return False


def _pepper_hash(pepper, value):
    if not pepper or not value:
        return None
    return hashlib.sha256((pepper + value).encode("utf-8")).hexdigest()


def _is_uuid_v4(s):
    if not isinstance(s, str) or len(s) != 36:
        return False
    try:
        u = _uuid.UUID(s)
    except (ValueError, AttributeError, TypeError):
        return False
    return u.version == 4 and str(u) == s.lower()


def _gen_case_ref():
    suffix = "".join(secrets.choice(_CROCKFORD) for _ in range(8))
    year = _dt.datetime.now(_dt.timezone.utc).year
    return f"KVSV-{year}-{suffix}"


def _validate_payload(payload):
    if not isinstance(payload, dict):
        return None, "invalid_body"
    if set(payload.keys()) - ALLOWED_FIELDS:
        return None, "unknown_fields"
    if not _is_uuid_v4(payload.get("idempotency_key")):
        return None, "invalid_idempotency"
    tt = payload.get("turnstile_token")
    if not isinstance(tt, str) or not tt:
        return None, "invalid_turnstile_token"
    cleaned = {}
    for f in STRING_FIELDS:
        if f in payload:
            v = payload[f]
            if not isinstance(v, str):
                return None, "invalid_type"
            v = v.strip()
            if len(v) > FIELD_MAX[f]:
                return None, "field_too_long"
            cleaned[f] = v
    for f in BOOL_FIELDS:
        if f in payload:
            v = payload[f]
            if not isinstance(v, bool):
                return None, "invalid_type"
            cleaned[f] = v
    for f in REQUIRED_STRINGS:
        if not cleaned.get(f):
            return None, "missing_required"
    if cleaned.get("category") not in CATEGORIES:
        return None, "invalid_category"
    for f in REQUIRED_BOOLS:
        if cleaned.get(f) is not True:
            return None, "acknowledgment_missing"
    cleaned.setdefault("contactConsent", False)
    cleaned.setdefault("publicationConsent", False)
    return {"cleaned": cleaned,
            "idempotency_key": payload["idempotency_key"],
            "turnstile_token": tt}, None


def _now_utc():
    return _dt.datetime.now(_dt.timezone.utc)


def _rate_limit_ref(db, kind, key_hash, window_name):
    return (db.collection("contribute_rate_limits")
              .document(kind)
              .collection(key_hash)
              .document(window_name))


def _as_utc(dt):
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=_dt.timezone.utc)
    return dt


@app.route("/api/contribute", methods=["POST"])
def contribute():
    if not request.is_json:
        return jsonify({"error": "The submission could not be processed."}), 415

    if request.content_length is not None and request.content_length > MAX_BODY_BYTES:
        return jsonify({"error": "The submission could not be processed."}), 413
    raw = request.get_data(cache=True)
    if len(raw) > MAX_BODY_BYTES:
        return jsonify({"error": "The submission could not be processed."}), 413
    try:
        payload = json.loads(raw.decode("utf-8")) if raw else None
    except Exception:
        return jsonify({"error": "The submission could not be processed."}), 400

    validated, err = _validate_payload(payload)
    if err:
        _log.info("contribute: validation failed: %s", err)
        return jsonify({"error": "The submission could not be processed."}), 400

    cleaned = validated["cleaned"]
    idem = validated["idempotency_key"]
    ip = request.remote_addr or ""

    if not _verify_turnstile(validated["turnstile_token"], ip):
        _log.info("contribute: turnstile rejected")
        return jsonify({"error": "The submission could not be processed."}), 400

    db = _get_contribute_firestore()
    if db is None:
        _log.error("contribute: firestore not configured")
        return jsonify({"error": "The submission could not be processed."}), 503

    ip_hash = _pepper_hash(CONTRIBUTE_IP_PEPPER, ip) if ip else None
    email_norm = cleaned["email"].lower()
    email_hash = _pepper_hash(CONTRIBUTE_EMAIL_PEPPER, email_norm)
    idem_hash = hashlib.sha256(idem.encode("utf-8")).hexdigest()

    submission_ref = db.collection("contribute_submissions").document()
    submission_id_stable = submission_ref.id
    case_reference_stable = _gen_case_ref()
    idem_ref = db.collection("contribute_idempotency").document(idem_hash)

    now_utc = _now_utc()
    now_epoch = int(now_utc.timestamp())
    idem_expires = now_utc + _dt.timedelta(seconds=IDEMPOTENCY_WINDOW_SECONDS)

    expiries_by_window = {}
    for kind, window_name, window_seconds, _ in RATE_LIMITS:
        expiries_by_window[(kind, window_name)] = (
            now_utc + _dt.timedelta(seconds=window_seconds)
        )

    limit_refs = []
    for kind, window_name, window_seconds, max_count in RATE_LIMITS:
        key_hash = ip_hash if kind == "ip" else email_hash
        if not key_hash:
            continue
        lref = _rate_limit_ref(db, kind, key_hash, window_name)
        limit_refs.append((kind, window_name, window_seconds, max_count, lref))

    @_fb_firestore.transactional
    def _tx(transaction):
        idem_snap = idem_ref.get(transaction=transaction)

        limit_reads = []
        for kind, window_name, window_seconds, max_count, lref in limit_refs:
            lsnap = lref.get(transaction=transaction)
            limit_reads.append(
                (kind, window_name, window_seconds, max_count, lref, lsnap)
            )

        if idem_snap.exists:
            data = idem_snap.to_dict() or {}
            ref = data.get("case_reference")
            expires_at = _as_utc(data.get("expiresAt"))
            if ref and expires_at is not None and expires_at > now_utc:
                return {"outcome": "already_received", "case_reference": ref}

        limit_plans = []
        for kind, window_name, window_seconds, max_count, lref, lsnap in limit_reads:
            if lsnap.exists:
                d = lsnap.to_dict() or {}
                ws = int(d.get("windowStartEpoch", 0))
                ct = int(d.get("count", 0))
                if now_epoch - ws >= window_seconds:
                    ws, ct = now_epoch, 0
            else:
                ws, ct = now_epoch, 0
            if ct >= max_count:
                return {"outcome": "rate_limited"}
            limit_plans.append((kind, window_name, lref, ws, ct))

        transaction.set(submission_ref, {
            "case_reference": case_reference_stable,
            "category": cleaned["category"],
            "name": cleaned["name"],
            "email": cleaned["email"],
            "phone": cleaned.get("phone", ""),
            "organization": cleaned.get("organization", ""),
            "family": cleaned.get("family", ""),
            "names": cleaned.get("names", ""),
            "relationships": cleaned.get("relationships", ""),
            "locations": cleaned.get("locations", ""),
            "dates": cleaned.get("dates", ""),
            "narrative": cleaned["narrative"],
            "research_question": cleaned.get("research_question", ""),
            "source_description": cleaned.get("source_description", ""),
            "document_reference": cleaned.get("document_reference", ""),
            "contactConsent": bool(cleaned["contactConsent"]),
            "publicationConsent": bool(cleaned["publicationConsent"]),
            "ackPublicRecord": True,
            "ackAccuracy": True,
            "status": "intake_review",
            "gate": "gate_0_intake_integrity",
            "assigned_tier": None,
            "final_disposition": None,
            "idempotency_hash": idem_hash,
            "created_at": _fb_firestore.SERVER_TIMESTAMP,
            "updated_at": _fb_firestore.SERVER_TIMESTAMP,
            "source": "kvsvsearch.github.io/contribute",
            "audit": [{
                "event": "created",
                "at": now_utc,
                "actor": "server",
                "note": "Contribution received through the public intake form.",
            }],
        })

        transaction.set(idem_ref, {
            "case_reference": case_reference_stable,
            "submission_id": submission_id_stable,
            "created_at": _fb_firestore.SERVER_TIMESTAMP,
            "expiresAt": idem_expires,
        })

        for kind, window_name, lref, ws, ct in limit_plans:
            transaction.set(lref, {
                "windowStartEpoch": ws,
                "count": ct + 1,
                "expiresAt": expiries_by_window[(kind, window_name)],
            })

        return {"outcome": "created",
                "case_reference": case_reference_stable,
                "submission_id": submission_id_stable}

    try:
        tx_result = _tx(db.transaction())
    except Exception:
        _log.exception("contribute: transaction failed")
        return jsonify({"error": "The submission could not be processed."}), 503

    outcome = tx_result.get("outcome")
    case_reference = tx_result.get("case_reference")

    if outcome == "rate_limited":
        return jsonify({"error": "Please try again later."}), 429

    if outcome == "already_received":
        return jsonify({
            "ok": True,
            "result": "already_received",
            "case_reference": case_reference,
        }), 200

    if outcome != "created":
        _log.error("contribute: unknown transaction outcome: %r", outcome)
        return jsonify({"error": "The submission could not be processed."}), 503

    submission_id = tx_result["submission_id"]
    received_at = _now_utc().strftime("%Y-%m-%dT%H:%M:%SZ")

    reviewer_ok = False
    if _SENDGRID_AVAILABLE and SENDGRID_API_KEY and SENDGRID_FROM and SENDGRID_TO:
        try:
            body = (
                f"Case reference: {case_reference}\n"
                f"Received: {received_at}\n"
                f"Category: {cleaned['category']}\n"
                f"Submitter name: {cleaned['name']}\n"
                f"Submitter email: {cleaned['email']}\n"
                f"Family/surname: {cleaned.get('family','') or '(none)'}\n"
                f"Contact consent: {bool(cleaned['contactConsent'])}\n"
                f"Publication consent: {bool(cleaned['publicationConsent'])}\n\n"
                f"Open Firestore Console, collection contribute_submissions, "
                f"and look up the document whose case_reference is "
                f"{case_reference}.\n"
            )
            SendGridAPIClient(SENDGRID_API_KEY).send(Mail(
                from_email=SENDGRID_FROM,
                to_emails=SENDGRID_TO,
                subject=f"KVSV Contribute -- new submission {case_reference}",
                plain_text_content=body,
            ))
            reviewer_ok = True
        except Exception:
            _log.exception("contribute: reviewer email send failed")

    submitter_ok = False
    if _SENDGRID_AVAILABLE and SENDGRID_API_KEY and SENDGRID_FROM:
        try:
            body = (
                f"We have received your submission.\n\n"
                f"Case reference: {case_reference}\n"
                f"Received: {received_at}\n"
                f"Category: {cleaned['category']}\n\n"
                f"Your submission is not automatically published. "
                f"KVSV researchers review contributions before any "
                f"publication decision.\n\n"
                f"For correction or deletion requests, contact "
                f"{CORRECTION_CONTACT}.\n"
            )
            SendGridAPIClient(SENDGRID_API_KEY).send(Mail(
                from_email=SENDGRID_FROM,
                to_emails=cleaned["email"],
                subject=f"KVSV Contribute -- receipt {case_reference}",
                plain_text_content=body,
            ))
            submitter_ok = True
        except Exception:
            _log.exception("contribute: submitter receipt send failed")

    email_audit_at = _now_utc()
    try:
        db.collection("contribute_submissions").document(submission_id).update({
            "updated_at": _fb_firestore.SERVER_TIMESTAMP,
            "audit": _fb_firestore.ArrayUnion([
                {"event": "reviewer_email_accepted_by_provider"
                    if reviewer_ok else "reviewer_email_provider_request_failed",
                 "at": email_audit_at,
                 "actor": "server"},
                {"event": "submitter_receipt_accepted_by_provider"
                    if submitter_ok else "submitter_receipt_provider_request_failed",
                 "at": email_audit_at,
                 "actor": "server"},
            ])
        })
    except Exception:
        _log.exception("contribute: audit write failed")

    return jsonify({
        "ok": True,
        "result": "created",
        "case_reference": case_reference,
    }), 200
