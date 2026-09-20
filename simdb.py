from flask import Flask, jsonify, request, Response
import requests
from bs4 import BeautifulSoup
from typing import List, Dict, Any
import re, os, time
from concurrent.futures import ThreadPoolExecutor

app = Flask(__name__)


@app.after_request
def add_cors(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "*"
    return response


TARGET_URL = "https://freshsimtracker.com/numberDetails.php"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Content-Type": "application/x-www-form-urlencoded",
    "Origin": "https://freshsimtracker.com",
    "Referer": "https://freshsimtracker.com/",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-User": "?1",
}

NETWORK_MAP = {
    "mob": "Jazz", "jazz": "Jazz", "mobilink": "Jazz",
    "zong": "Zong", "ufone": "Ufone", "telenor": "Telenor",
    "warid": "Warid", "scom": "SCOM",
}

_cache: Dict[str, tuple] = {}
CACHE_TTL = 120


def cache_get(key):
    if key in _cache:
        val, ts = _cache[key]
        if time.time() - ts < CACHE_TTL:
            return val
        del _cache[key]
    return None


def cache_set(key, val):
    _cache[key] = (val, time.time())


# ---------- Cleaning ----------
NULL_PAT = re.compile(r"\bnull\b", re.IGNORECASE)
WS_PAT = re.compile(r"\s+")


def clean_text(s: str) -> str:
    if not s:
        return ""
    return WS_PAT.sub(" ", NULL_PAT.sub("", s)).strip(" -.,")


def clean_name(s: str) -> str:
    s = clean_text(s)
    if not s or s.upper() in {"UNKNOWN", "N/A", "NA", "DATA NOT RECIEVED FROM NADRA",
                              "DATA NOT RECEIVED FROM NADRA", "NO"}:
        return ""
    return s


def clean_address(s: str) -> str:
    s = clean_text(s)
    return "" if s.lower() in {"no", "n/a", "null"} else s


def normalize_network(s: str) -> str:
    if not s:
        return ""
    key = s.lower().replace(".png", "").replace(".jpg", "").strip()
    return NETWORK_MAP.get(key, s.title())


# ---------- Variant normalizer ----------
def normalize_variants(raw: str) -> List[str]:
    q = re.sub(r"[\s\-\(\)]", "", raw.strip()).lstrip("+")

    if q.isdigit() and len(q) == 13:
        return [q, f"{q[:5]}-{q[5:12]}-{q[12:]}"]

    if q.startswith("92") and len(q) == 12:
        q = "0" + q[2:]

    if q.isdigit():
        if len(q) == 11 and q.startswith("0"):
            return [q, q[1:]]
        if len(q) == 10:
            return ["0" + q, q]

    return [raw.strip()]


# ---------- Fetch / parse ----------
class DataSourceError(Exception):
    def __init__(self, detail):
        self.detail = detail
        super().__init__(detail)


def fetch_raw(query: str) -> str:
    session = requests.Session()
    session.headers.update(HEADERS)
    try:
        r = session.post(
            TARGET_URL,
            data={"numberCnic": query, "searchNumber": "search"},
            timeout=20,
        )
        r.raise_for_status()
        return r.text
    except requests.RequestException as e:
        raise DataSourceError(f"Data source unavailable: {e}")


def parse_table_format(soup: BeautifulSoup) -> List[Dict[str, Any]]:
    records = []
    table = soup.find("table", class_="table")
    if not table:
        return records
    for row in table.find_all("tr"):
        cells = row.find_all("td")
        if len(cells) < 4:
            continue
        img = cells[4].find("img") if len(cells) > 4 else None
        raw_net = ""
        if img and img.get("src"):
            raw_net = img["src"].split("/")[-1].rsplit(".", 1)[0]
        records.append({
            "number":  clean_text(cells[0].get_text(strip=True)),
            "name":    clean_name(cells[1].get_text(strip=True)),
            "cnic":    clean_text(cells[2].get_text(strip=True)),
            "address": clean_address(cells[3].get_text(strip=True)),
            "network": normalize_network(raw_net),
        })
    return records


def parse_certificate_format(soup: BeautifulSoup) -> Dict[str, Any]:
    rec = {"number": "", "name": "", "cnic": "", "address": "", "network": ""}
    for row in soup.find_all("tr"):
        cells = row.find_all("td")
        if cells and cells[0].get_text(strip=True) == "MSISDN" and len(cells) > 1:
            rec["number"] = clean_text(cells[1].get_text(strip=True))
    text = soup.get_text("\n", strip=True)
    m = re.search(r"on account of income tax has been\s*\n\s*(.+)", text)
    if m:
        rec["name"] = clean_name(m.group(1))
    m = re.search(r"deducted/collected from\s*\n\s*(.+)", text)
    if m:
        rec["address"] = clean_address(m.group(1))
    m = re.search(r"holder of CNIC No\.?\s*\n\s*([0-9\-]+)", text)
    if m:
        rec["cnic"] = clean_text(m.group(1))
    return rec


def parse_response(html: str) -> List[Dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    recs = parse_table_format(soup)
    if recs:
        return recs
    single = parse_certificate_format(soup)
    return [single] if any(single.values()) else []


def dedupe(records):
    seen = {}
    for r in records:
        key = (r["number"], r["name"], r["cnic"], r["address"], r["network"])
        if key in seen:
            seen[key]["duplicate_count"] += 1
        else:
            r["duplicate_count"] = 1
            seen[key] = r
    return list(seen.values())


def summarize(records):
    numbers = {r["number"] for r in records if r["number"]}
    networks = {r["network"] for r in records if r["network"]}
    named = [r for r in records if r["name"]]
    return {
        "total_records": len(records),
        "unique_numbers": len(numbers),
        "unique_networks": len(networks),
        "named_records": len(named),
        "unknown_records": len(records) - len(named),
        "networks": sorted(networks),
    }


# ---------- Single search ----------
def do_search_single(query: str) -> Dict[str, Any]:
    query = query.strip()
    if not query or len(query) < 7:
        return {
            "query": query, "success": False, "records": [],
            "error": "Invalid input", "summary": summarize([]),
            "variants_tried": [], "matched_variant": None,
            "fetch_error": None,
        }

    cached = cache_get(query)
    if cached:
        return cached

    variants = normalize_variants(query)
    all_records, matched = [], None
    fetch_error = None

    for v in variants:
        try:
            recs = parse_response(fetch_raw(v))
        except DataSourceError as e:
            fetch_error = getattr(e, "detail", str(e))
            recs = []
        if recs:
            all_records, matched = recs, v
            break
        time.sleep(0.1)

    records = dedupe(all_records)
    result = {
        "query": query,
        "variants_tried": variants,
        "matched_variant": matched,
        "summary": summarize(records),
        "records": records,
        "success": len(records) > 0,
        "fetch_error": fetch_error if not records else None,
    }
    cache_set(query, result)
    return result


# ---------- Bulk search ----------
def do_search_bulk(queries: List[str]) -> Dict[str, Any]:
    queries = [q.strip() for q in queries if q.strip()][:25]
    results = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(do_search_single, q): q for q in queries}
        for fut in futures:
            try:
                results.append(fut.result(timeout=30))
            except Exception:
                results.append({
                    "query": futures[fut], "success": False, "records": [],
                    "summary": summarize([]), "variants_tried": [],
                    "matched_variant": None, "fetch_error": "Timeout",
                })
    total = sum(len(r["records"]) for r in results)
    return {"total_queries": len(queries), "total_records": total, "results": results}


# ---------- Routes ----------
@app.route("/api/health")
def health():
    return jsonify({"status": "ok", "service": "sim-lookup", "time": int(time.time())})


@app.route("/api/search")
def api_search():
    query = request.args.get("query", "")
    return jsonify(do_search_single(query))


@app.route("/api/bulk", methods=["POST"])
def api_bulk():
    payload = request.get_json(silent=True) or {}
    queries = payload.get("queries", [])
    if not queries:
        return jsonify({"detail": "No queries provided"}), 400
    return jsonify(do_search_bulk(queries))


@app.route("/")
def index():
    path = os.path.join(os.path.dirname(__file__), "templates", "index.html")
    with open(path, "r", encoding="utf-8") as f:
        return Response(f.read(), mimetype="text/html")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
