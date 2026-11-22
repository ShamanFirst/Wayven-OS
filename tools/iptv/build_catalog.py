#!/usr/bin/env python3
"""Build the internet TV channel catalogue for Wayven OS (iptv-catalog-*.json.gz + manifest).

Sources:
  * iptv-org (github.com/iptv-org): community list of publicly available TV streams, data under
    the Unlicense (public domain). Used through its API files (channels, feeds, streams, logos,
    blocklist). The streams themselves belong to their broadcasters; the catalogue only holds
    the addresses they published.
  * Wikidata (CC0): description, owner and launch year of channels that can be matched by web
    address or by name and country.

What the build does:
  1. downloads the iptv-org files and the Wikidata TV channel items;
  2. leaves out every adult channel (is_nsfw, and the blocklist with all its reasons),
     channels that closed, streams without a channel (they cannot be checked for adult
     content) and streams the device cannot open (RTMP, RTSP, SRT);
  3. groups the streams of a channel: quality feeds (SD, HD) become variants of one entry,
     regional or language feeds become entries of their own;
  4. checks every stream address once (it must answer with an HLS or DASH playlist) and
     records the result; the device shows its own live check as a dot next to each channel;
  5. sorts the variants (working first, then the one closest to 720p), adds the Wikidata facts
     and writes a gzip-compressed JSON file with a manifest (size, SHA-256), signed with the
     catalogue key (ECDSA P-256, openssl).

Logos are not copied: the catalogue holds the address iptv-org lists, and the device fetches the
picture from there when it shows the channel.

Usage:
  python3 tools/iptv/build_catalog.py --out DIR [--key key.pem] [--no-probe] [--src DIR]
  (--src reuses downloaded iptv-org files and wikidata-tv.json, for tests without the network)
"""
import argparse
import concurrent.futures
import datetime
import gzip
import hashlib
import http.client
import json
import os
import re
import ssl
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
AGENT = "WayvenCatalogBuilder/1.0 (+https://github.com/ShamanFirst/Wayven-OS)"
API = "https://iptv-org.github.io/api/"
API_FILES = ("channels", "feeds", "streams", "logos", "blocklist", "categories")
WIKIDATA = "https://query.wikidata.org/sparql"
FORMAT = 1
QUALITY_FEEDS = {"SD", "HD", "FHD", "UHD", "4K", "HEVC", "MAIN"}
MAX_TEXT = 160
PROBE_WORKERS = 64
PROBE_TIMEOUT = 10.0

# iptv-org uses a few codes that are not ISO 3166-1.
COUNTRY_ALIASES = {"UK": "GB"}

# Flags of a stream variant.
F_GEO = 1          # the provider allows only some countries
F_NOT_247 = 2      # not on air all day
F_DASH = 4         # MPEG-DASH instead of HLS

WIKIDATA_QUERY = """
SELECT ?item ?site ?country ?inception ?ownerLabel ?descPl ?descEn ?labelPl ?labelEn WHERE {
  ?item wdt:P31/wdt:P279* wd:Q2001305 .
  ?item wdt:P17 ?c . ?c wdt:P297 ?country .
  OPTIONAL { ?item wdt:P856 ?site }
  OPTIONAL { ?item wdt:P571 ?inception }
  OPTIONAL { ?item wdt:P127 ?owner . ?owner rdfs:label ?ownerLabel FILTER(LANG(?ownerLabel) = "en") }
  OPTIONAL { ?item schema:description ?descPl FILTER(LANG(?descPl) = "pl") }
  OPTIONAL { ?item schema:description ?descEn FILTER(LANG(?descEn) = "en") }
  OPTIONAL { ?item rdfs:label ?labelPl FILTER(LANG(?labelPl) = "pl") }
  OPTIONAL { ?item rdfs:label ?labelEn FILTER(LANG(?labelEn) = "en") }
}
"""


def log(*args):
    print(*args, file=sys.stderr, flush=True)


def tls_context():
    ctx = ssl.create_default_context()
    if not ctx.get_ca_certs() and os.path.exists("/etc/ssl/cert.pem"):
        # The python.org build on macOS ships without CA certificates; the system bundle works.
        ctx.load_verify_locations("/etc/ssl/cert.pem")
    return ctx


TLS = tls_context()
# Stream servers often have expired or self-made certificates; the device's player (ffmpeg) does
# not check them, so the stream check does not either.
TLS_STREAMS = ssl.create_default_context()
TLS_STREAMS.check_hostname = False
TLS_STREAMS.verify_mode = ssl.CERT_NONE


def http_get(url, timeout=300, headers=None):
    req = urllib.request.Request(url, headers=dict({"User-Agent": AGENT}, **(headers or {})))
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=TLS) as resp:
            return resp.read()
    except (urllib.error.URLError, ssl.SSLError) as exc:
        log("urllib failed (%s), trying curl" % exc)
        cmd = ["curl", "-sfL", "-m", str(timeout), "-A", AGENT]
        for key, value in (headers or {}).items():
            cmd += ["-H", "%s: %s" % (key, value)]
        return subprocess.run(cmd + [url], capture_output=True, check=True).stdout


# ---------------------------------------------------------------- sources

def fetch_sources(src_dir):
    data = {}
    for name in API_FILES:
        if src_dir:
            with open(os.path.join(src_dir, name + ".json"), encoding="utf-8") as f:
                data[name] = json.load(f)
        else:
            log("downloading %s.json" % name)
            data[name] = json.loads(http_get(API + name + ".json"))
    path = os.path.join(src_dir, "wikidata-tv.json") if src_dir else ""
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            data["wikidata"] = json.load(f)
    elif src_dir:
        data["wikidata"] = []
    else:
        log("asking Wikidata")
        raw = None
        for attempt in range(3):
            try:
                raw = http_get(WIKIDATA + "?" + urllib.parse.urlencode({"query": WIKIDATA_QUERY}), timeout=120,
                               headers={"Accept": "application/sparql-results+json"})
                break
            except (OSError, subprocess.CalledProcessError) as exc:
                log("Wikidata attempt %d failed: %s" % (attempt + 1, exc))
                time.sleep(20)
        data["wikidata"] = json.loads(raw)["results"]["bindings"] if raw else []
    return data


# ---------------------------------------------------------------- normalising

def clean_text(text, max_len=MAX_TEXT):
    text = " ".join(str(text or "").split())
    return text if len(text) <= max_len else text[:max_len - 1].rstrip() + "…"


def name_key(name):
    name = unicodedata.normalize("NFKD", name or "").casefold()
    name = "".join(c for c in name if not unicodedata.combining(c))
    name = re.sub(r"\b(tv|hd|fhd|sd|channel|television|telewizja)\b", " ", name)
    return re.sub(r"[^0-9a-zЀ-ӿ؀-ۿ぀-鿿]+", "", name)


def domain_of(url):
    try:
        host = urllib.parse.urlsplit(url or "").hostname or ""
    except ValueError:
        return ""
    host = host.lower()
    return host[4:] if host.startswith("www.") else host


def quality_height(text):
    m = re.match(r"(\d{3,4})[pi]", text or "")
    return int(m.group(1)) if m else 0


def wikidata_index(rows):
    """Wikidata facts by (domain) and by (normalised name, country)."""
    items = {}
    for row in rows:
        qid = row["item"]["value"].rsplit("/", 1)[-1]
        item = items.setdefault(qid, {"q": qid, "sites": set(), "names": set(), "cc": set()})
        get = lambda key: row.get(key, {}).get("value")  # noqa: E731
        if get("site"):
            item["sites"].add(domain_of(get("site")))
        if get("country"):
            item["cc"].add(get("country").upper())
        for key in ("labelPl", "labelEn"):
            if get(key):
                item["names"].add(name_key(get(key)))
        if get("descPl"):
            item["dpl"] = get("descPl")
        if get("descEn"):
            item["den"] = get("descEn")
        if get("ownerLabel"):
            item["owner"] = get("ownerLabel")
        if get("inception") and get("inception")[:4].isdigit():
            item["year"] = int(get("inception")[:4])
    by_site = {}
    by_name = {}
    for item in items.values():
        for site in item["sites"]:
            if site:
                by_site.setdefault(site, []).append(item)
        for name in item["names"]:
            for cc in item["cc"]:
                if name:
                    by_name.setdefault((name, cc), []).append(item)
    return by_site, by_name


def wikidata_facts(channel, cc, by_site, by_name):
    found = None
    site = domain_of(channel.get("website"))
    candidates = by_site.get(site, []) if site else []
    if len(candidates) > 1:
        candidates = [c for c in candidates if cc in c["cc"]] or candidates[:0]
    if len(candidates) == 1:
        found = candidates[0]
    if found is None:
        candidates = by_name.get((name_key(channel["name"]), cc), [])
        if len(candidates) == 1:
            found = candidates[0]
    if found is None:
        return None
    facts = {"q": found["q"]}
    if found.get("dpl") or found.get("den"):
        facts["d"] = clean_text(found.get("dpl") or found.get("den"))
    if found.get("owner"):
        facts["o"] = clean_text(found["owner"], 60)
    if found.get("year"):
        facts["y"] = found["year"]
    return facts


# ---------------------------------------------------------------- stream check

def probe(stream):
    """True when the address answers with an HLS or DASH playlist (as the device would ask)."""
    headers = {"User-Agent": stream.get("user_agent") or "Mozilla/5.0 (X11; Linux aarch64) Wayven/1.0"}
    if stream.get("referrer"):
        headers["Referer"] = stream["referrer"]
    req = urllib.request.Request(stream["url"], headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=PROBE_TIMEOUT, context=TLS_STREAMS) as resp:
            if resp.status != 200:
                return False
            head = resp.read(8192)
    except (OSError, ssl.SSLError, ValueError, http.client.HTTPException):
        return False
    return b"#EXTM3U" in head[:1024] or b"<MPD" in head


def probe_all(streams, workers=PROBE_WORKERS):
    results = {}
    done = [0]
    lock = threading.Lock()
    started = time.time()

    def one(index):
        ok = probe(streams[index])
        with lock:
            results[index] = ok
            done[0] += 1
            if done[0] % 1000 == 0:
                log("checked %d/%d streams (%.0f s)" % (done[0], len(streams), time.time() - started))

    with concurrent.futures.ThreadPoolExecutor(workers) as pool:
        list(pool.map(one, range(len(streams))))
    return results


# ---------------------------------------------------------------- build

def build(data, countries, check=True):
    channels = {c["id"]: c for c in data["channels"]}
    blocked = {b["channel"] for b in data["blocklist"]}
    feeds = {(f["channel"], f["id"]): f for f in data["feeds"]}
    main_feed = {f["channel"]: f["id"] for f in data["feeds"] if f.get("is_main")}
    by_site, by_name = wikidata_index(data["wikidata"])
    stats = {"streams_in": len(data["streams"]), "channels_in": len(channels), "left_out": {}}

    def skip(reason):
        stats["left_out"][reason] = stats["left_out"].get(reason, 0) + 1

    logos = {}
    for logo in data["logos"]:
        if not logo.get("in_use", True) or (logo.get("format") or "").upper() == "SVG":
            continue
        key = (logo["channel"], logo.get("feed"))
        best = logos.get(key)
        # Wider logos read better in the small box; very large files are not needed.
        score = min(logo.get("width") or 0, 600)
        if best is None or score > best[0]:
            logos[key] = (score, logo["url"])

    usable = []
    for stream in data["streams"]:
        cid = stream.get("channel")
        if not cid:
            skip("no_channel")
            continue
        channel = channels.get(cid)
        if channel is None:
            skip("unknown_channel")
            continue
        if channel.get("is_nsfw") or cid in blocked:
            skip("adult_or_blocked")
            continue
        if channel.get("closed"):
            skip("closed")
            continue
        scheme = urllib.parse.urlsplit(stream["url"]).scheme.lower()
        if scheme not in ("http", "https"):
            skip("protocol")
            continue
        usable.append(stream)

    working = probe_all(usable) if check else {}
    stats["checked"] = len(working)
    stats["working"] = sum(1 for ok in working.values() if ok)

    entries = {}
    for index, stream in enumerate(usable):
        cid = stream["channel"]
        channel = channels[cid]
        feed_id = stream.get("feed") or main_feed.get(cid) or ""
        feed = feeds.get((cid, feed_id), {})
        # Quality feeds of one channel are one entry; a regional feed is an entry of its own.
        separate = feed_id and not feed.get("is_main") and feed_id.upper() not in QUALITY_FEEDS
        key = cid + ("@" + feed_id if separate else "")
        entry = entries.get(key)
        if entry is None:
            cc = COUNTRY_ALIASES.get(channel["country"], channel["country"])
            name = clean_text(channel["name"], 60)
            if separate:
                name = clean_text("%s (%s)" % (name, feed.get("name") or feed_id), 70)
            entry = entries[key] = {
                "id": key, "n": name, "c": cc if cc in countries else "ZZ",
                "g": [g for g in channel.get("categories") or []][:4],
                "l": (feed.get("languages") or [])[:3],
                "s": [],
            }
            logo = logos.get((cid, feed_id if separate else None)) or logos.get((cid, None))
            if logo is None:
                logo = next((v for (c, _), v in logos.items() if c == cid), None)
            if logo:
                entry["logo"] = logo[1]
            facts = wikidata_facts(channel, entry["c"], by_site, by_name)
            if facts:
                entry["wd"] = facts
            if channel.get("website"):
                entry["w"] = channel["website"][:120]
        flags = 0
        labels = stream.get("labels") or []
        if "Geo-blocked" in labels:
            flags |= F_GEO
        if "Not 24/7" in labels:
            flags |= F_NOT_247
        if ".mpd" in stream["url"].split("?")[0].lower():
            flags |= F_DASH
        variant = {"u": stream["url"], "h": quality_height(stream.get("quality")), "f": flags}
        if stream.get("user_agent"):
            variant["ua"] = stream["user_agent"][:200]
        if stream.get("referrer"):
            variant["r"] = stream["referrer"][:200]
        if index in working:
            variant["ok"] = 1 if working[index] else 0
        if not any(v["u"] == variant["u"] for v in entry["s"]):
            entry["s"].append(variant)

    def variant_rank(v):
        h = v["h"] or 576
        # Working first; then the closest to 720 lines without going over, then the smallest above.
        return (-v.get("ok", 1), 0 if h <= 720 else 1, -h if h <= 720 else h, v["f"] & F_GEO)

    out = []
    for entry in entries.values():
        entry["s"].sort(key=variant_rank)
        entry["s"] = entry["s"][:6]
        if check:
            entry["ok"] = max(v.get("ok", 0) for v in entry["s"])
        out.append(entry)
    out.sort(key=lambda e: (e["c"], -e.get("ok", 1), e["n"].casefold()))

    per_country = {}
    for entry in out:
        per_country[entry["c"]] = per_country.get(entry["c"], 0) + 1
    stats.update({
        "entries": len(out),
        "entries_working": sum(1 for e in out if e.get("ok", 1)),
        "with_wikidata": sum(1 for e in out if "wd" in e),
        "with_logo": sum(1 for e in out if "logo" in e),
        "countries": len(per_country),
    })
    return out, per_country, stats


def sign(path, key):
    sig = path[:-len(".json")] + ".sig"
    subprocess.run(["openssl", "dgst", "-sha256", "-sign", key, "-out", sig, path], check=True)
    return sig


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True)
    ap.add_argument("--key", help="ECDSA P-256 private key (PEM) for the manifest signature")
    ap.add_argument("--src", help="directory with downloaded iptv-org files (and wikidata-tv.json)")
    ap.add_argument("--no-probe", action="store_true", help="do not check the stream addresses")
    args = ap.parse_args()

    with open(os.path.join(HERE, "..", "radio", "countries.json"), encoding="utf-8") as f:
        countries = json.load(f)
    os.makedirs(args.out, exist_ok=True)
    data = fetch_sources(args.src)
    if not args.src:
        with open(os.path.join(args.out, "wikidata-tv.json"), "w", encoding="utf-8") as f:
            json.dump(data["wikidata"], f, ensure_ascii=False)
    entries, per_country, stats = build(data, countries, check=not args.no_probe)

    now = datetime.datetime.now(datetime.timezone.utc)
    version = now.strftime("%Y.%m.%d.%H%M")
    country_rows = {}
    for cc, count in per_country.items():
        info = countries.get(cc) or {"pl": "Bez kraju", "en": "No country", "continent": "WW"}
        country_rows[cc] = {"pl": info["pl"] or info["en"], "en": info["en"], "k": info["continent"] or "WW",
                            "n": count}
    catalogue = {
        "format": FORMAT, "version": version, "built": int(now.timestamp()),
        "sources": ["iptv-org (Unlicense)", "Wikidata (CC0)"],
        "categories": {c["id"]: c["name"] for c in data["categories"]},
        "countries": country_rows,
        "channels": entries,
    }
    raw = json.dumps(catalogue, ensure_ascii=False, separators=(",", ":")).encode()
    blob = gzip.compress(raw, 9, mtime=0)
    file_name = "iptv-catalog-%s.json.gz" % version
    with open(os.path.join(args.out, file_name), "wb") as f:
        f.write(blob)
    manifest = {
        "format": FORMAT, "version": version, "date": now.strftime("%Y-%m-%d"), "built": int(now.timestamp()),
        "channels": len(entries), "countries": len(per_country), "file": file_name, "size": len(blob),
        "sha256": hashlib.sha256(blob).hexdigest(), "sources": catalogue["sources"],
    }
    man_path = os.path.join(args.out, "iptv-manifest.json")
    with open(man_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=1)
    if args.key:
        sign(man_path, args.key)
    stats.update({"bytes": len(blob), "bytes_raw": len(raw), "version": version})
    with open(os.path.join(args.out, "iptv-stats.json"), "w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False, indent=1)
    log(json.dumps(stats, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
