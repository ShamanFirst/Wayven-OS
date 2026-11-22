#!/usr/bin/env python3
"""Programme guide (EPG) for the internet TV of Wayven OS: tv-epg-*.json.gz + signed manifest.

Two steps, both run by the daily GitHub job (Wayven-OS/.github/workflows/tv-epg.yml):

  channels  writes channels.xml for the iptv-org EPG grabber (github.com/iptv-org/epg): one guide
            source per channel of the TV catalogue that answered at its last check, taken from
            iptv-org's guides.json (sites with many channels first, they are the best kept);
  pack      reads the grabber's XMLTV output and writes, per catalogue channel, the programmes
            from one hour ago to 36 hours ahead: start and end (Unix time, UTC) and the title
            (at most 70 characters; the description is left out), gzip-compressed, with a
            manifest (size, SHA-256) signed with the catalogue key.

Usage:
  python3 tools/iptv/build_epg.py channels --catalog iptv-catalog.json.gz --guides guides.json --out channels.xml
  python3 tools/iptv/build_epg.py pack --guide guide.xml --out DIR [--key key.pem]
"""
import argparse
import collections
import datetime
import gzip
import hashlib
import json
import os
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape

FORMAT = 1
TITLE_MAX = 70
PAST_S = 3600
AHEAD_S = 36 * 3600


def log(*args):
    print(*args, file=sys.stderr, flush=True)


def write_channels(catalog_path, guides_path, out_path, limit):
    with gzip.open(catalog_path) as f:
        catalog = json.load(f)
    with open(guides_path, encoding="utf-8") as f:
        guides = json.load(f)
    wanted = collections.OrderedDict()
    for entry in catalog["channels"]:
        if entry.get("ok", 1):
            wanted.setdefault(entry["id"].split("@")[0], entry["id"])
    site_size = collections.Counter(g["site"] for g in guides if g["channel"] in wanted)
    best = {}
    for g in guides:
        cid = g["channel"]
        if cid not in wanted or not g.get("site_id"):
            continue
        score = site_size[g["site"]]
        if cid not in best or score > best[cid][0]:
            best[cid] = (score, g)
    picked = list(best.values())[:limit] if limit else list(best.values())
    lines = ['<?xml version="1.0" encoding="UTF-8"?>', "<channels>"]
    for _, g in picked:
        lines.append('  <channel site="%s" lang="%s" xmltv_id="%s" site_id="%s">%s</channel>' % (
            escape(g["site"], {'"': "&quot;"}), escape(g.get("lang") or "en", {'"': "&quot;"}),
            escape(g["channel"], {'"': "&quot;"}), escape(str(g["site_id"]), {'"': "&quot;"}),
            escape(g.get("site_name") or g["channel"])))
    lines.append("</channels>")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    log("channels.xml: %d channels from %d sites" % (len(picked), len({g["site"] for _, g in picked})))


def parse_time(text):
    # XMLTV: "20261122193000 +0100"
    text = (text or "").strip()
    try:
        stamp = datetime.datetime.strptime(text[:14], "%Y%m%d%H%M%S")
    except ValueError:
        return None
    offset = 0
    rest = text[14:].strip()
    if len(rest) == 5 and rest[0] in "+-":
        offset = (int(rest[1:3]) * 3600 + int(rest[3:5]) * 60) * (1 if rest[0] == "+" else -1)
    return int(stamp.replace(tzinfo=datetime.timezone.utc).timestamp()) - offset


def pack(guide_path, out_dir, key):
    now = int(time.time())
    programmes = collections.defaultdict(list)
    opener = gzip.open if guide_path.endswith(".gz") else open
    with opener(guide_path, "rb") as f:
        for _, elem in ET.iterparse(f, events=("end",)):
            if elem.tag != "programme":
                continue
            start = parse_time(elem.get("start"))
            stop = parse_time(elem.get("stop"))
            channel = elem.get("channel") or ""
            title_el = elem.find("title")
            title = " ".join((title_el.text or "").split()) if title_el is not None else ""
            elem.clear()
            if not channel or not title or start is None:
                continue
            if stop is None or stop <= start:
                stop = start + 1800
            if stop < now - PAST_S or start > now + AHEAD_S:
                continue
            if len(title) > TITLE_MAX:
                title = title[:TITLE_MAX - 1].rstrip() + "…"
            programmes[channel].append([start, stop, title])
    out = {}
    for channel, items in programmes.items():
        items.sort()
        clean = []
        for item in items:
            if clean and item[0] < clean[-1][1]:
                if item[0] == clean[-1][0]:
                    continue          # the same slot twice
                clean[-1][1] = item[0]
            clean.append(item)
        out[channel] = clean
    built = datetime.datetime.now(datetime.timezone.utc)
    version = built.strftime("%Y.%m.%d.%H%M")
    data = {"format": FORMAT, "version": version, "built": now, "channels": out}
    blob = gzip.compress(json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode(), 9, mtime=0)
    os.makedirs(out_dir, exist_ok=True)
    name = "tv-epg-%s.json.gz" % version
    with open(os.path.join(out_dir, name), "wb") as f:
        f.write(blob)
    manifest = {"format": FORMAT, "version": version, "built": now, "channels": len(out),
                "programmes": sum(len(v) for v in out.values()), "file": name, "size": len(blob),
                "sha256": hashlib.sha256(blob).hexdigest(), "sources": ["iptv-org/epg (guide sites listed there)"]}
    man_path = os.path.join(out_dir, "tv-epg-manifest.json")
    with open(man_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=1)
    if key:
        subprocess.run(["openssl", "dgst", "-sha256", "-sign", key, "-out",
                        os.path.join(out_dir, "tv-epg-manifest.sig"), man_path], check=True)
    log(json.dumps({k: manifest[k] for k in ("version", "channels", "programmes", "size")}))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("channels")
    c.add_argument("--catalog", required=True)
    c.add_argument("--guides", required=True)
    c.add_argument("--out", required=True)
    c.add_argument("--limit", type=int, default=0, help="at most this many channels (tests)")
    p = sub.add_parser("pack")
    p.add_argument("--guide", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--key")
    args = ap.parse_args()
    if args.cmd == "channels":
        write_channels(args.catalog, args.guides, args.out, args.limit)
    else:
        pack(args.guide, args.out, args.key)


if __name__ == "__main__":
    main()
