#!/usr/bin/env python3
"""Build the internet radio station catalogue for Wayven OS (radio-catalog.bin + manifest).

Sources (both free to use in commercial products):
  * Radio Browser (radio-browser.info): community database. Its API page says the service is
    free, may be used in free and non-free software, and all its data may be mirrored.
  * Wikidata: structured data under CC0 (items with a "streaming media URL", P963).

What the build does:
  1. downloads the station list of Radio Browser (one request, broken stations left out) and the
     Wikidata items that carry a stream address;
  2. keeps only what the device plays: MP3, Ogg (Vorbis, Opus) and FLAC over HTTP(S). AAC is left
     out unless --aac is given (patent licence, see docs/adr/0009); HLS and video are left out;
  3. removes duplicates: the same stream address entered twice, and the same station entered with
     several streams, which become quality variants of one station (merged by normalised name,
     country and web domain);
  4. adds Wikidata stations that Radio Browser lacks after checking that their stream answers
     with audio;
  5. sorts each country by popularity, writes the compact binary catalogue (one zlib block per
     country) and a manifest with its size and SHA-256, signed with ECDSA P-256 (openssl).

Station logos are NOT copied: the catalogue holds the address the station published, and the
device fetches the picture from there while it plays.

Usage:
  python3 tools/radio/build_catalog.py --out DIR [--key key.pem] [--aac] [--rb-json file]
  (--rb-json/--wd-json reuse downloaded source files, for tests without the network)
"""
import argparse
import concurrent.futures
import datetime
import hashlib
import json
import math
import os
import random
import re
import socket
import ssl
import struct
import subprocess
import sys
import tempfile
import time
import unicodedata
import urllib.parse
import urllib.request
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
AGENT = "WayvenCatalogBuilder/1.0 (+https://github.com/ShamanFirst/Wayven-OS)"
FORMAT = 1
MAX_PER_COUNTRY = 2500       # bounds the RAM one country list takes on the device
MAX_BYTES = 1800000          # a catalogue slot on the device is 1.75 MB including a 4 kB record
MAX_VARIANTS = 4
MAX_URL = 200                # the record stores lengths in one byte
MAX_NAME_BYTES = 64
MAX_STATE_BYTES = 40

CODEC_MP3, CODEC_OGG, CODEC_FLAC, CODEC_AAC, CODEC_HLS = 1, 2, 3, 4, 5
CODEC_NAMES = {CODEC_MP3: "MP3", CODEC_OGG: "OGG", CODEC_FLAC: "FLAC", CODEC_AAC: "AAC", CODEC_HLS: "HLS"}
RB_CODECS = {"MP3": CODEC_MP3, "OGG": CODEC_OGG, "FLAC": CODEC_FLAC, "AAC": CODEC_AAC, "AAC+": CODEC_AAC}

CONTINENTS = ["EU", "AS", "AF", "NA", "SA", "OC", "AN", "WW"]   # WW: stations without a country
NO_COUNTRY = "ZZ"

# Genre vocabulary shown on the device (Polish names). Tags of the sources map onto it.
GENRES = [
    ("pop", "Pop"),
    ("rock", "Rock"),
    ("alt", "Alternatywa"),
    ("metal", "Metal"),
    ("dance", "Dance i elektronika"),
    ("hiphop", "Hip-hop i R&B"),
    ("jazz", "Jazz i blues"),
    ("classical", "Muzyka klasyczna"),
    ("oldies", "Przeboje dawnych lat"),
    ("chill", "Relaks"),
    ("news", "Informacje i rozmowy"),
    ("sport", "Sport"),
    ("religion", "Religijne"),
    ("folk", "Folk i muzyka świata"),
    ("country", "Country"),
    ("latin", "Latynoska"),
    ("reggae", "Reggae"),
    ("schlager", "Szlagiery i disco"),
    ("kids", "Dla dzieci"),
    ("culture", "Kultura i edukacja"),
    ("soundtrack", "Muzyka filmowa"),
]
GENRE_INDEX = {key: i for i, (key, _) in enumerate(GENRES)}

# Phrase -> genres. Longer phrases are tried first, so "classic rock" never reads as "classical".
TAG_RULES = {
    "classic rock": ["rock", "oldies"], "classic hits": ["oldies"], "classical": ["classical"],
    "classic": ["oldies"], "klassik": ["classical"], "opera": ["classical"], "baroque": ["classical"],
    "symphon": ["classical"], "orchestra": ["classical"], "muzyka klasyczna": ["classical"],
    "pop": ["pop"], "top 40": ["pop"], "top40": ["pop"], "hits": ["pop"], "charts": ["pop"],
    "adult contemporary": ["pop"], "hot ac": ["pop"], "contemporary": ["pop"],
    "rock": ["rock"], "hard rock": ["rock"], "progressive": ["rock"],
    "alternative": ["alt"], "indie": ["alt"], "punk": ["alt"], "grunge": ["alt"], "new wave": ["alt"],
    "metal": ["metal"],
    "dance": ["dance"], "electronic": ["dance"], "electro": ["dance"], "edm": ["dance"], "house": ["dance"],
    "techno": ["dance"], "trance": ["dance"], "club": ["dance"], "drum and bass": ["dance"], "dnb": ["dance"],
    "dubstep": ["dance"], "eurodance": ["dance"], "hardstyle": ["dance"],
    "hip hop": ["hiphop"], "hip-hop": ["hiphop"], "hiphop": ["hiphop"], "rap": ["hiphop"], "r&b": ["hiphop"],
    "rnb": ["hiphop"], "urban": ["hiphop"], "soul": ["hiphop"], "funk": ["hiphop"],
    "jazz": ["jazz"], "blues": ["jazz"], "swing": ["jazz"],
    "oldies": ["oldies"], "60s": ["oldies"], "70s": ["oldies"], "80s": ["oldies"], "90s": ["oldies"],
    "retro": ["oldies"], "golden": ["oldies"], "evergreen": ["oldies"], "nostalgia": ["oldies"],
    "chillout": ["chill"], "chill": ["chill"], "lounge": ["chill"], "ambient": ["chill"], "relax": ["chill"],
    "easy listening": ["chill"], "smooth": ["chill"], "downtempo": ["chill"], "meditation": ["chill"],
    "news": ["news"], "talk": ["news"], "information": ["news"], "informacje": ["news"],
    "nachrichten": ["news"], "politics": ["news"], "current affairs": ["news"], "public radio": ["news"],
    "sport": ["sport"], "football": ["sport"], "soccer": ["sport"],
    "christian": ["religion"], "religio": ["religion"], "catholic": ["religion"], "katolick": ["religion"],
    "gospel": ["religion"], "worship": ["religion"], "church": ["religion"], "quran": ["religion"],
    "islam": ["religion"], "bible": ["religion"], "religia": ["religion"],
    "folk": ["folk"], "traditional": ["folk"], "folklore": ["folk"], "world music": ["folk"],
    "ethnic": ["folk"], "celtic": ["folk"], "ludow": ["folk"],
    "country": ["country"], "bluegrass": ["country"], "americana": ["country"],
    "latin": ["latin"], "salsa": ["latin"], "reggaeton": ["latin"], "bachata": ["latin"], "cumbia": ["latin"],
    "tango": ["latin"], "latino": ["latin"],
    "reggae": ["reggae"], "ska": ["reggae"], "dancehall": ["reggae"],
    "schlager": ["schlager"], "disco polo": ["schlager"], "disco": ["schlager"], "volksmusik": ["schlager"],
    "chanson": ["schlager"], "italo": ["schlager"],
    "kids": ["kids"], "children": ["kids"], "kinder": ["kids"], "dzieci": ["kids"],
    "culture": ["culture"], "kultura": ["culture"], "education": ["culture"], "university": ["culture"],
    "college": ["culture"], "student": ["culture"], "literature": ["culture"], "arts": ["culture"],
    "soundtrack": ["soundtrack"], "film": ["soundtrack"], "movie": ["soundtrack"],
}
TAG_PATTERNS = [(re.compile(r"(?<![a-z0-9])" + re.escape(p)), g)
                for p, g in sorted(TAG_RULES.items(), key=lambda kv: -len(kv[0]))]

# Words that say something about the stream, not the station: removed before names are compared.
NOISE_WORDS = {"mp3", "aac", "aacplus", "ogg", "opus", "flac", "hq", "lq", "hd", "hifi", "stream", "mobile",
               "low", "high", "kbps", "kbit", "kbits", "lossless"}
GENERIC_NAMES = {"radio", "fm", "am", "live", "stream", "webradio", "onlineradio", "music", "radiofm",
                 "internetradio", "test", "musica", "musik", "muzyka"}
RADIO_WORDS = r"radio|rádio|radyo|radiyo|rádió|radijas|radijo|rozhlas"
GENERIC_PATHS = {"/stream", "/live", "/listen", "/radio", "/stream/1", "/mp3", "/stream.mp3", "/live.mp3", "/high",
                 "/low", "/stream/;", "/;stream.mp3", "/;stream", "/autodj", "/main", "/audio", "/stream1",
                 "/stream2", "/listen/stream", "/radio.mp3", "/live/stream", "/stream/stream", "/icecast"}
SECOND_LEVEL = {"co", "com", "org", "net", "gov", "edu", "ac", "or", "ne", "go"}


def log(*args):
    print(*args, file=sys.stderr, flush=True)


def tls_context():
    ctx = ssl.create_default_context()
    if not ctx.get_ca_certs() and os.path.exists("/etc/ssl/cert.pem"):
        # The python.org build on macOS ships without CA certificates; the system bundle works.
        ctx.load_verify_locations("/etc/ssl/cert.pem")
    return ctx


TLS = tls_context()


# ---------------------------------------------------------------- downloads

def http_get(url, timeout=300):
    req = urllib.request.Request(url, headers={"User-Agent": AGENT, "Accept-Encoding": "gzip"})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=TLS) as resp:
            data = resp.read()
            if resp.headers.get("Content-Encoding") == "gzip":
                data = zlib.decompress(data, 16 + zlib.MAX_WBITS)
            return data
    except (urllib.error.URLError, ssl.SSLError) as exc:
        # The python.org build on macOS has no CA certificates: curl has the system's.
        log("urllib failed (%s), trying curl" % exc)
        out = subprocess.run(["curl", "-sfL", "--compressed", "-m", str(timeout), "-A", AGENT, url],
                             capture_output=True, check=True)
        return out.stdout


def rb_servers():
    try:
        servers = json.loads(http_get("https://all.api.radio-browser.info/json/servers", 30))
        names = sorted({s["name"] for s in servers if s.get("name")})
    except Exception as exc:  # noqa: BLE001 - any failure: fall back to the known names
        log("server list failed: %s" % exc)
        names = []
    # The list sometimes names a single server; the known ones are always tried too.
    names = sorted(set(names) | {"de1.api.radio-browser.info", "de2.api.radio-browser.info",
                                 "fi1.api.radio-browser.info"})
    random.shuffle(names)
    return names


def fetch_radio_browser():
    last = None
    for attempt in range(3):
        for server in rb_servers():
            url = "https://%s/json/stations?hidebroken=true&limit=200000" % server
            try:
                log("Radio Browser: %s" % url)
                data = json.loads(http_get(url))
                if len(data) > 10000:
                    return data
                last = "only %d stations" % len(data)
            except Exception as exc:  # noqa: BLE001
                last = exc
            log("  failed: %s" % last)
        time.sleep(30 * (attempt + 1))
    raise SystemExit("Radio Browser unreachable: %s" % last)


WD_QUERY = """
SELECT ?item ?url ?iso ?labelpl ?labelen ?label ?home WHERE {
  ?item wdt:P963 ?url .
  FILTER NOT EXISTS { ?item wdt:P31/wdt:P279* wd:Q2001305 }
  FILTER NOT EXISTS { ?item wdt:P31/wdt:P279* wd:Q1616075 }
  OPTIONAL { ?item wdt:P17 ?c . ?c wdt:P297 ?iso }
  OPTIONAL { ?item rdfs:label ?labelpl FILTER(LANG(?labelpl) = "pl") }
  OPTIONAL { ?item rdfs:label ?labelen FILTER(LANG(?labelen) = "en") }
  OPTIONAL { ?item rdfs:label ?label }
  OPTIONAL { ?item wdt:P856 ?home }
}"""


def fetch_wikidata():
    url = "https://query.wikidata.org/sparql?format=json&query=" + urllib.parse.quote(WD_QUERY)
    for attempt in range(4):
        try:
            data = json.loads(http_get(url, 180))
            items = {}
            for b in data["results"]["bindings"]:
                qid = b["item"]["value"].rsplit("/", 1)[-1]
                entry = items.setdefault(qid, {"qid": qid, "urls": [], "iso": "", "name": "", "home": ""})
                u = b["url"]["value"]
                if u not in entry["urls"]:
                    entry["urls"].append(u)
                if not entry["iso"] and "iso" in b:
                    entry["iso"] = b["iso"]["value"].upper()
                for key in ("labelpl", "labelen", "label"):
                    if key in b and not entry["name"]:
                        entry["name"] = b[key]["value"]
                if not entry["home"] and "home" in b:
                    entry["home"] = b["home"]["value"]
            return list(items.values())
        except Exception as exc:  # noqa: BLE001
            log("Wikidata attempt %d failed: %s" % (attempt + 1, exc))
            time.sleep(10 * (attempt + 1))
    log("Wikidata unavailable: the catalogue is built from Radio Browser alone")
    return []


# ---------------------------------------------------------------- stream check (Wikidata entries)

def probe_stream(url, timeout=8.0, redirects=4):
    """Connects like the device does (HTTP/1.0, ICY accepted) and returns (codec, kbps) or None."""
    for _ in range(redirects + 1):
        parts = urllib.parse.urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            return None
        port = parts.port or (443 if parts.scheme == "https" else 80)
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        try:
            sock = socket.create_connection((parts.hostname, port), timeout=timeout)
            if parts.scheme == "https":
                sock = TLS.wrap_socket(sock, server_hostname=parts.hostname)
            req = ("GET %s HTTP/1.0\r\nHost: %s\r\nUser-Agent: %s\r\nIcy-MetaData: 0\r\nAccept: */*\r\n\r\n"
                   % (path, parts.hostname, AGENT))
            sock.sendall(req.encode())
            head = b""
            while b"\r\n\r\n" not in head and len(head) < 8192:
                chunk = sock.recv(2048)
                if not chunk:
                    break
                head += chunk
            sock.close()
        except (OSError, ssl.SSLError):
            return None
        lines = head.split(b"\r\n\r\n", 1)[0].decode("latin-1").split("\r\n")
        m = re.match(r"^(HTTP/1\.[01]|ICY) (\d{3})", lines[0]) if lines else None
        if not m:
            return None
        status = int(m.group(2))
        headers = {}
        for line in lines[1:]:
            if ":" in line:
                k, v = line.split(":", 1)
                headers[k.strip().lower()] = v.strip()
        if status in (301, 302, 303, 307, 308) and "location" in headers:
            url = urllib.parse.urljoin(url, headers["location"])
            continue
        if status != 200:
            return None
        ctype = headers.get("content-type", "").split(";")[0].strip().lower()
        codec = {"audio/mpeg": CODEC_MP3, "audio/mp3": CODEC_MP3, "audio/ogg": CODEC_OGG,
                 "application/ogg": CODEC_OGG, "audio/flac": CODEC_FLAC, "audio/x-flac": CODEC_FLAC,
                 "audio/aac": CODEC_AAC, "audio/aacp": CODEC_AAC}.get(ctype)
        if codec is None:
            return None
        try:
            kbps = int(re.match(r"\d+", headers.get("icy-br", "0")).group(0))
        except (AttributeError, ValueError):
            kbps = 0
        return codec, kbps, url
    return None


# ---------------------------------------------------------------- normalising

def clean_text(text, max_bytes):
    text = "".join(ch for ch in unicodedata.normalize("NFC", text or "") if unicodedata.category(ch)[0] != "C")
    text = re.sub(r"\s+", " ", text).strip(" -|·•_")
    data = text.encode("utf-8")
    if len(data) > max_bytes:
        data = data[:max_bytes]
        while data and (data[-1] & 0xC0) == 0x80:
            data = data[:-1]
        if data and data[-1] >= 0xC0:
            data = data[:-1]
        text = data.decode("utf-8", "ignore").rstrip()
    return text


def display_name(name):
    """Station name without the stream remarks some entries carry ("- 128k", "[MP3]")."""
    name = clean_text(name, 200)
    stripped = name
    while True:
        shorter = re.sub(r"[\s\-|/(\[]*\b(\d{2,3}\s?k(bps|b/s|bit)?|mp3|aac\+?|ogg|opus|flac|hq|lq|hd)\b[\])]*\s*$",
                         "", stripped, flags=re.I)
        if shorter == stripped:
            break
        stripped = shorter
    # A bracket left open by the cut ("Radio Szczecin (128K").
    if stripped.count("(") > stripped.count(")"):
        stripped = stripped[:stripped.rfind("(")]
    return clean_text(stripped or name, MAX_NAME_BYTES)


def name_key(name):
    text = unicodedata.normalize("NFKD", (name or "").casefold())
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.replace("&", " and ")
    inner = re.sub(r"[\(\[].*?[\)\]]", " ", text)
    if re.search(r"\w", inner):
        text = inner
    words = re.findall(r"\w+", text)
    # Numbers belong to names ("Antena 1", "Radio 357"); only a bit rate ("128k", "128 kbps") goes.
    text = re.sub(r"\b\d{2,3}\s*(k|kb|kbps|kbit|kbit/s|kb/s)\b", " ", " ".join(words))
    words = [w for w in text.split() if w not in NOISE_WORDS]
    return "".join(words)


def url_key(url):
    p = urllib.parse.urlsplit(url.strip())
    host = (p.hostname or "").lower()
    port = p.port
    if port in (80, 443):
        port = None
    path = re.sub(r"/+$", "", p.path or "")
    path = re.sub(r";$", "", path)
    # Session and listener tokens make the same stream look different.
    query = "&".join(sorted(q for q in p.query.split("&") if q and not re.match(
        r"(?i)(sid|session|token|uid|listenerid|ts|t|_|nocache|cb|aw_0_\w+|ref|type|icy)=", q)))
    return "%s%s%s%s" % (host, ":%d" % port if port else "", path, "?" + query if query else "")


def domain_of(url):
    host = (urllib.parse.urlsplit(url or "").hostname or "").lower()
    host = re.sub(r"^(www\d?|m|mobile|web)\.", "", host)
    labels = host.split(".")
    if len(labels) >= 3 and labels[-2] in SECOND_LEVEL:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:]) if len(labels) >= 2 else host


def genres_of(tags):
    mask = 0
    for tag in (tags or "").lower().split(","):
        tag = tag.strip()
        if not tag:
            continue
        for pattern, genres in TAG_PATTERNS:
            if pattern.search(tag):
                for g in genres:
                    mask |= 1 << GENRE_INDEX[g]
                break
    return mask


def fnv1a(text):
    h = 0x811C9DC5
    for b in text.encode():
        h = ((h ^ b) * 0x01000193) & 0xFFFFFFFF
    return h


# ---------------------------------------------------------------- merging

class UnionFind:
    def __init__(self, n):
        self.parent = list(range(n))

    def find(self, a):
        while self.parent[a] != a:
            self.parent[a] = self.parent[self.parent[a]]
            a = self.parent[a]
        return a

    def union(self, a, b):
        a, b = self.find(a), self.find(b)
        if a != b:
            self.parent[max(a, b)] = min(a, b)


def popularity(entry):
    return entry.get("clickcount", 0) + 0.05 * entry.get("votes", 0) + 0.2 * max(entry.get("clicktrend", 0), 0)


def build(rb, wd, countries, allow_aac, probe_workers=24, allow_hls=False):
    stats = {"rb_input": len(rb), "wd_input": len(wd)}
    allowed = {CODEC_MP3, CODEC_OGG, CODEC_FLAC} | ({CODEC_AAC} if allow_aac else set())
    entries = []
    dropped = {"codec": 0, "aac": 0, "hls": 0, "url": 0, "name": 0, "tls": 0}
    for s in rb:
        codec_name = (s.get("codec") or "").upper()
        codec = RB_CODECS.get(codec_name)
        if codec == CODEC_AAC and not allow_aac:
            dropped["aac"] += 1
            continue
        url = (s.get("url_resolved") or s.get("url") or "").strip()
        hls = bool(s.get("hls")) or url.lower().split("?")[0].endswith(".m3u8")
        if hls:
            # HLS (playlists of segments, AAC or MP3 inside): played by firmware 0.7 and later.
            if not allow_hls:
                dropped["hls"] += 1
                continue
            codec = CODEC_HLS
        elif codec not in allowed:
            dropped["codec"] += 1
            continue
        if not re.match(r"^https?://[^\s]+$", url) or len(url) > MAX_URL or (not hls and url.lower().split("?")[0].endswith(
                (".m3u8", ".m3u", ".pls", ".asx", ".xspf"))):
            dropped["url"] += 1
            continue
        if url.startswith("https://") and s.get("ssl_error"):
            # The device checks certificates; a broken one would never play.
            dropped["tls"] += 1
            continue
        name = display_name(s.get("name", ""))
        if not name:
            dropped["name"] += 1
            continue
        cc = (s.get("countrycode") or "").upper()
        if cc not in countries:
            cc = NO_COUNTRY
        entries.append({
            "uuid": s.get("stationuuid", ""), "name": name, "key": name_key(name), "url": url, "ukey": url_key(url),
            "codec": codec, "kbps": int(s.get("bitrate") or 0), "cc": cc,
            "home": s.get("homepage") or "", "logo": (s.get("favicon") or "").strip(),
            "state": clean_text(s.get("state", ""), MAX_STATE_BYTES), "genres": genres_of(s.get("tags", "")),
            "pop": popularity(s), "wd": False,
        })
    stats["dropped"] = dropped

    # Wikidata: attach to an existing entry when the stream or the name matches, else check it.
    by_ukey = {}
    by_name = {}
    for i, e in enumerate(entries):
        by_ukey.setdefault(e["ukey"], i)
        if e["key"]:
            by_name.setdefault((e["key"], e["cc"]), i)
    to_probe = []
    wd_matched = 0
    for item in wd:
        cc = item["iso"] if item["iso"] in countries else NO_COUNTRY
        name = display_name(item["name"])
        hit = None
        for u in item["urls"]:
            hit = by_ukey.get(url_key(u))
            if hit is not None:
                break
        if hit is None and name:
            hit = by_name.get((name_key(name), cc))
        if hit is not None:
            entries[hit]["wd"] = True
            if not entries[hit]["home"]:
                entries[hit]["home"] = item["home"]
            wd_matched += 1
        elif name:
            for u in item["urls"][:2]:
                if re.match(r"^https?://[^\s]+$", u) and len(u) <= MAX_URL:
                    to_probe.append((item, name, cc, u))
    stats["wd_matched"] = wd_matched
    wd_added = 0
    if to_probe:
        log("checking %d Wikidata streams" % len(to_probe))
        with concurrent.futures.ThreadPoolExecutor(probe_workers) as pool:
            results = list(pool.map(lambda t: probe_stream(t[3]), to_probe))
        seen = set()
        for (item, name, cc, url), res in zip(to_probe, results):
            if res is None or res[0] not in allowed or item["qid"] in seen:
                continue
            final_url = res[2] if len(res[2]) <= MAX_URL else url
            seen.add(item["qid"])
            entries.append({
                "uuid": item["qid"], "name": name, "key": name_key(name), "url": final_url,
                "ukey": url_key(final_url), "codec": res[0], "kbps": res[1], "cc": cc, "home": item["home"],
                "logo": "", "state": "", "genres": 0, "pop": 0.0, "wd": True,
            })
            wd_added += 1
    stats["wd_added"] = wd_added

    # Duplicates: same stream, or same station (name + country + web domain).
    uf = UnionFind(len(entries))
    first_url = {}
    for i, e in enumerate(entries):
        j = first_url.setdefault(e["ukey"], i)
        if j != i:
            uf.union(i, j)
    buckets = {}
    for i, e in enumerate(entries):
        if e["key"] and len(e["key"]) >= 4 and e["key"] not in GENERIC_NAMES:
            buckets.setdefault((e["key"], e["cc"]), []).append(i)
    for (key, _), members in buckets.items():
        if len(members) < 2:
            continue
        if len(key) >= 5:
            # A distinctive name in one country is one station, whatever web address each entry
            # gives ("ESKA Do Pracy" was entered with eska.pl and eskago.pl).
            for i in members[1:]:
                uf.union(members[0], i)
            continue
        clusters = {}
        loose = []
        for i in members:
            d = domain_of(entries[i]["home"])
            (clusters.setdefault(d, []) if d else loose).append(i)
        for group in clusters.values():
            for i in group[1:]:
                uf.union(group[0], i)
        if loose:
            # Entries without a web address join the most popular site of that name; with no site at
            # all, they are one station.
            target = max(clusters.values(), key=lambda g: max(entries[i]["pop"] for i in g))[0] if clusters \
                else loose[0]
            for i in loose:
                uf.union(target, i)
    # The same name with and without the word "radio" ("RMF FM", "Radio RMF FM"), when the stream
    # or the web site is the same.
    short = {}
    for i, e in enumerate(entries):
        k = re.sub(RADIO_WORDS, "", e["key"])
        if len(k) >= 3 and k not in GENERIC_NAMES:
            for d in {domain_of(e["url"]), domain_of(e["home"])} - {""}:
                j = short.setdefault((k, e["cc"], d), i)
                if j != i:
                    uf.union(i, j)
    # Mirrors of one stream on a CDN: same provider domain and the same specific path.
    mirrors = {}
    for i, e in enumerate(entries):
        path = urllib.parse.urlsplit(e["url"]).path.rstrip("/")
        if len(path) >= 6 and path.lower() not in GENERIC_PATHS and re.search(r"[a-z0-9]{4}", path.lower()):
            j = mirrors.setdefault((domain_of(e["url"]), path), i)
            if j != i:
                uf.union(i, j)
    groups = {}
    for i in range(len(entries)):
        groups.setdefault(uf.find(i), []).append(i)
    stats["entries_after_filter"] = len(entries)
    stats["stations"] = len(groups)

    stations = []
    used_ids = set()
    for members in groups.values():
        members.sort(key=lambda i: -entries[i]["pop"])
        top = entries[members[0]]
        variants = {}
        for i in members:
            e = entries[i]
            if e["ukey"] not in variants or e["pop"] > variants[e["ukey"]]["pop"]:
                variants[e["ukey"]] = e
        vs = list(variants.values())
        # One variant per codec and bit rate (the most popular), then at most four spread over
        # the range: highest, lowest and the most popular ones between.
        by_q = {}
        for v in vs:
            q = (v["codec"], v["kbps"])
            if q not in by_q or v["pop"] > by_q[q]["pop"]:
                by_q[q] = v
        vs = sorted(by_q.values(), key=lambda v: -v["kbps"])
        if len(vs) > MAX_VARIANTS:
            keep = [vs[0], vs[-1]] + sorted(vs[1:-1], key=lambda v: -v["pop"])[:MAX_VARIANTS - 2]
            vs = sorted(keep, key=lambda v: -v["kbps"])
        logo = ""
        for i in members:
            cand = entries[i]["logo"]
            if re.match(r"^https?://", cand) and len(cand) <= MAX_URL and not re.search(r"\.svg(\?|$)", cand, re.I):
                logo = cand
                break
        genres = 0
        for i in members:
            genres |= entries[i]["genres"]
        state = next((entries[i]["state"] for i in members if entries[i]["state"]), "")
        sid_src = min(entries[i]["uuid"] for i in members)
        sid = fnv1a(sid_src)
        salt = 0
        while sid in used_ids or sid == 0:
            salt += 1
            sid = fnv1a("%s#%d" % (sid_src, salt))
        used_ids.add(sid)
        pop = sum(entries[i]["pop"] for i in members)
        stations.append({
            "id": sid, "name": top["name"], "cc": top["cc"], "state": state, "logo": logo, "genres": genres,
            "score": min(65535, int(math.log1p(pop) * 5000)), "wd": any(entries[i]["wd"] for i in members),
            "variants": [(v["codec"], v["kbps"], v["url"]) for v in vs],
        })
    stats["variants_total"] = sum(len(s["variants"]) for s in stations)
    stats["multi_variant_stations"] = sum(1 for s in stations if len(s["variants"]) > 1)
    return stations, stats


# ---------------------------------------------------------------- binary

def lp(text, max_len=255):
    data = text.encode("utf-8")[:max_len]
    return struct.pack("<B", len(data)) + data


def encode(stations, countries, built, allow_aac, cap=MAX_PER_COUNTRY):
    by_cc = {}
    for s in stations:
        by_cc.setdefault(s["cc"], []).append(s)
    country_rows = []
    for cc, items in by_cc.items():
        items.sort(key=lambda s: (-s["score"], s["name"].casefold()))
        del items[cap:]
        if cc == NO_COUNTRY:
            name, cont = "Bez kraju (internetowe)", "WW"
        else:
            name, cont = countries[cc]["pl"] or countries[cc]["en"], countries[cc]["continent"] or "WW"
        country_rows.append((cont, name, cc, items))
    country_rows.sort(key=lambda r: (CONTINENTS.index(r[0]), r[1].casefold()))

    strings = bytearray()
    for _, label in GENRES:
        strings += label.encode() + b"\0"
    blocks = bytearray()
    table = bytearray()
    total = 0
    for cont, name, cc, items in country_rows:
        name_off = len(strings)
        strings += name.encode() + b"\0"
        raw = bytearray()
        gmask = 0
        for s in items:
            flags = 1 if s["wd"] else 0
            raw += struct.pack("<IHIB", s["id"], s["score"], s["genres"], flags)
            raw += lp(s["name"]) + lp(s["state"]) + lp(s["logo"])
            raw += struct.pack("<B", len(s["variants"]))
            for codec, kbps, url in s["variants"]:
                raw += struct.pack("<BH", codec, min(kbps, 65535)) + lp(url)
            gmask |= s["genres"]
        comp = zlib.compress(bytes(raw), 9)
        table += struct.pack("<2s2sIIIIHHI", cc.encode(), cont.encode(), name_off, len(blocks), len(comp), len(raw),
                             len(items), 0, gmask)
        blocks += comp
        total += len(items)
    header_len = 64
    countries_off = header_len
    strings_off = countries_off + len(table)
    blocks_off = strings_off + len(strings)
    total_size = blocks_off + len(blocks)
    body = bytes(table) + bytes(strings) + bytes(blocks)
    crc = zlib.crc32(body) & 0xFFFFFFFF
    header = struct.pack("<4sHHIIHHIIIIII", b"WRC1", FORMAT, 1 if allow_aac else 0, built, total, len(country_rows),
                         len(GENRES), countries_off, strings_off, len(strings), blocks_off, total_size, crc)
    header += b"\0" * (header_len - len(header))
    return header + body, total, len(country_rows)


def decode_check(blob):
    """Reads the file back the way the device does, to catch format mistakes before publishing."""
    magic, fmt, flags, built, total, ncountry, ngenre, c_off, s_off, s_size, b_off, size, crc = struct.unpack_from(
        "<4sHHIIHHIIIIII", blob, 0)
    assert magic == b"WRC1" and fmt == FORMAT and size == len(blob)
    assert zlib.crc32(blob[64:]) & 0xFFFFFFFF == crc
    count = 0
    for i in range(ncountry):
        cc, cont, name_off, boff, csize, usize, n, _, gmask = struct.unpack_from("<2s2sIIIIHHI", blob, c_off + 28 * i)
        raw = zlib.decompress(blob[b_off + boff:b_off + boff + csize])
        assert len(raw) == usize
        pos = 0
        for _ in range(n):
            pos += 11
            for _ in range(3):
                pos += 1 + raw[pos]
            nv = raw[pos]
            pos += 1
            assert 1 <= nv <= MAX_VARIANTS
            for _ in range(nv):
                pos += 3
                pos += 1 + raw[pos]
        assert pos == usize, (cc, pos, usize)
        count += n
    assert count == total
    return True


def sign(path, key):
    sig = path[:-len(".json")] + ".sig" if path.endswith(".json") else path + ".sig"
    subprocess.run(["openssl", "dgst", "-sha256", "-sign", key, "-out", sig, path], check=True)
    return sig


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True)
    ap.add_argument("--key", help="ECDSA P-256 private key (PEM) for the manifest signature")
    ap.add_argument("--aac", action="store_true", help="include AAC streams (needs an AAC patent licence)")
    ap.add_argument("--hls", action="store_true", help="include HLS streams (firmware 0.7 and later)")
    ap.add_argument("--suffix", default="", help="added to the file names, e.g. -aac for the list of newer firmware")
    ap.add_argument("--rb-json", help="use this Radio Browser station list instead of downloading it")
    ap.add_argument("--wd-json", help="use this Wikidata list (from a previous build) instead of the query")
    ap.add_argument("--no-probe", action="store_true", help="do not check Wikidata-only streams")
    ap.add_argument("--font-json", help="file/size/sha256/version of the fonts for other scripts, put into the manifest")
    args = ap.parse_args()

    with open(os.path.join(HERE, "countries.json"), encoding="utf-8") as f:
        countries = json.load(f)
    os.makedirs(args.out, exist_ok=True)
    rb = json.load(open(args.rb_json, encoding="utf-8")) if args.rb_json else fetch_radio_browser()
    if args.wd_json:
        wd = json.load(open(args.wd_json, encoding="utf-8"))
    else:
        wd = fetch_wikidata()
        with open(os.path.join(args.out, "wikidata-streams.json"), "w", encoding="utf-8") as f:
            json.dump(wd, f, ensure_ascii=False)
    if args.no_probe:
        wd = [w for w in wd if False]  # noqa: C416 - keep the type, drop the entries

    stations, stats = build(rb, wd, countries, args.aac, allow_hls=args.hls)
    now = datetime.datetime.now(datetime.timezone.utc)
    cap = MAX_PER_COUNTRY
    while True:
        # Fewer stations per country (the least popular go) if the file would not fit a slot.
        blob, total, ncountry = encode([dict(st) for st in stations], countries, int(now.timestamp()), args.aac, cap)
        if len(blob) <= MAX_BYTES or cap <= 300:
            break
        cap -= 200
        log("catalogue %d bytes, too large: at most %d stations per country" % (len(blob), cap))
    decode_check(blob)
    # A new name for each version: the old file stays downloadable until the new manifest is up,
    # so a device never pairs a manifest with the wrong file.
    # Hour and minute too: a second build on the same day must count as newer on the device
    # (which keeps at most 15 characters of the version).
    version = now.strftime("%Y.%m.%d.%H%M")
    bin_name = "radio-catalog%s-%s.bin" % (args.suffix, version)
    bin_path = os.path.join(args.out, bin_name)
    with open(bin_path, "wb") as f:
        f.write(blob)
    manifest = {
        "format": FORMAT,
        "version": version,
        "date": now.strftime("%Y-%m-%d"),
        "built": int(now.timestamp()),
        "stations": total,
        "countries": ncountry,
        "aac": bool(args.aac),
        "file": bin_name,
        "size": len(blob),
        "sha256": hashlib.sha256(blob).hexdigest(),
        "sources": ["Radio Browser (radio-browser.info)", "Wikidata (CC0)"],
    }
    if args.font_json:
        with open(args.font_json, encoding="utf-8") as f:
            font = json.load(f)
        manifest["font"] = {k: font[k] for k in ("file", "size", "sha256", "version")}
    man_path = os.path.join(args.out, "radio-manifest%s.json" % args.suffix)
    with open(man_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=1)
    if args.key:
        sign(man_path, args.key)
    stats.update({"stations_written": total, "countries": ncountry, "bytes": len(blob)})
    with open(os.path.join(args.out, "radio-stats%s.json" % args.suffix), "w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False, indent=1)
    log(json.dumps(stats, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
