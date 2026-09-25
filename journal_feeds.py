#!/usr/bin/env python3
"""Build RSS 2.0 feeds of new journal articles from the Crossref API.

Writes one feed per journal to docs/, plus docs/feeds.opml for importing
all of them into a feed reader at once. Uses only the Python standard library.
"""

import html
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import format_datetime

# ---------------------------------------------------------------------------
# Journals to track. Add or remove lines freely; any ISSN of the journal works.
# slug = file name of the feed (docs/<slug>.xml)
# ---------------------------------------------------------------------------
JOURNALS = [
    # slug,          name,                              ISSN,        homepage
    ("aer",          "American Economic Review",        "0002-8282", "https://www.aeaweb.org/journals/aer"),
    ("aer-insights", "AER: Insights",                   "2640-205X", "https://www.aeaweb.org/journals/aeri"),
    ("aej-macro",    "AEJ: Macroeconomics",             "1945-7707", "https://www.aeaweb.org/journals/mac"),
    ("qje",          "Quarterly Journal of Economics",  "0033-5533", "https://academic.oup.com/qje"),
    ("restud",       "Review of Economic Studies",      "0034-6527", "https://academic.oup.com/restud"),
    ("rfs",          "Review of Financial Studies",     "0893-9454", "https://academic.oup.com/rfs"),
]

ROWS = 40  # most recent articles kept in each feed
OUT_DIR = "docs"

# Titles that are journal housekeeping rather than research articles.
SKIP_TITLE = re.compile(
    r"^(front matter|back matter|issue information|masthead|editorial board|"
    r"erratum|corrigendum|correction|retraction|announcements?|"
    r"report of the|minutes of|recent referees|referees|foreword|"
    r"table of contents|index)\b",
    re.IGNORECASE,
)

MAILTO = os.environ.get("CROSSREF_MAILTO", "").strip()
USER_AGENT = "econ-journal-feeds/1.0" + (f" (mailto:{MAILTO})" if MAILTO else "")


def fetch_works(issn):
    params = {
        "sort": "created",
        "order": "desc",
        "rows": str(ROWS),
        "filter": "type:journal-article",
        "select": "DOI,title,subtitle,author,abstract,created,URL,container-title",
    }
    if MAILTO:
        params["mailto"] = MAILTO
    url = (f"https://api.crossref.org/journals/{issn}/works?"
           + urllib.parse.urlencode(params))
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    last_err = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.load(resp)["message"]["items"]
        except Exception as err:  # network hiccup or Crossref overload
            last_err = err
            time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"Crossref request failed for ISSN {issn}: {last_err}")


def clean_abstract(raw):
    if not raw:
        return ""
    text = re.sub(r"<jats:title>.*?</jats:title>", " ", raw, flags=re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return re.sub(r"^Abstract\s*[:.]?\s*", "", text, flags=re.I)


def format_authors(authors):
    names = []
    for a in authors or []:
        name = " ".join(p for p in (a.get("given"), a.get("family")) if p)
        name = name or a.get("name", "")
        if name:
            names.append(name)
    return ", ".join(names)


def to_entry(item):
    title = " ".join(item.get("title") or []).strip()
    subtitle = " ".join(item.get("subtitle") or []).strip()
    if subtitle:
        title = f"{title}: {subtitle}"
    title = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", title)))
    authors = format_authors(item.get("author"))
    if not title or not authors or SKIP_TITLE.match(title):
        return None
    doi = item["DOI"]
    created = item.get("created", {}).get("date-time") or \
        datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "id": f"https://doi.org/{doi}",
        "title": title,
        "authors": authors,
        "abstract": clean_abstract(item.get("abstract")),
        "updated": created,
    }


def rfc822(iso):
    dt = datetime.strptime(iso[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    return format_datetime(dt, usegmt=True)


def write_rss(path, feed_url, name, homepage, entries):
    atom = "http://www.w3.org/2005/Atom"
    dc = "http://purl.org/dc/elements/1.1/"
    ET.register_namespace("atom", atom)
    ET.register_namespace("dc", dc)
    rss = ET.Element("rss", version="2.0")
    ch = ET.SubElement(rss, "channel")
    ET.SubElement(ch, "title").text = name
    ET.SubElement(ch, "link").text = homepage
    ET.SubElement(ch, "description").text = f"New articles in {name} (via Crossref)"
    ET.SubElement(ch, f"{{{atom}}}link", href=feed_url, rel="self",
                  type="application/rss+xml")
    now = format_datetime(datetime.now(timezone.utc), usegmt=True)
    ET.SubElement(ch, "lastBuildDate").text = now
    for e in entries:
        it = ET.SubElement(ch, "item")
        ET.SubElement(it, "title").text = e["title"]
        ET.SubElement(it, "link").text = e["id"]
        ET.SubElement(it, "guid", isPermaLink="true").text = e["id"]
        ET.SubElement(it, "pubDate").text = rfc822(e["updated"])
        ET.SubElement(it, f"{{{dc}}}creator").text = e["authors"]
        desc = f"<p>{html.escape(e['authors'])}</p>"
        if e["abstract"]:
            desc += f"<p>{html.escape(e['abstract'])}</p>"
        ET.SubElement(it, "description").text = desc
    ET.ElementTree(rss).write(path, encoding="utf-8", xml_declaration=True)


def base_url():
    # e.g. GITHUB_REPOSITORY="pmolavi/econ-feeds" -> https://pmolavi.github.io/econ-feeds/
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if "/" in repo:
        owner, name = repo.split("/", 1)
        return f"https://{owner.lower()}.github.io/{name}/"
    return "./"


def write_opml(path, base):
    root = ET.Element("opml", version="2.0")
    head = ET.SubElement(root, "head")
    ET.SubElement(head, "title").text = "Economics journals (Crossref)"
    body = ET.SubElement(root, "body")
    folder = ET.SubElement(body, "outline", text="Crossref journals")
    for slug, name, _, _ in JOURNALS:
        ET.SubElement(folder, "outline", type="rss", text=name, title=name,
                      xmlUrl=f"{base}{slug}.xml")
    ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    base = base_url()
    failures = 0
    for slug, name, issn, homepage in JOURNALS:
        try:
            items = fetch_works(issn)
        except RuntimeError as err:
            # Keep the previous feed file so the reader just sees no update.
            print(f"WARNING: {err}", file=sys.stderr)
            failures += 1
            continue
        entries = [e for e in (to_entry(i) for i in items) if e]
        write_rss(os.path.join(OUT_DIR, f"{slug}.xml"),
                  f"{base}{slug}.xml", name, homepage, entries)
        print(f"{name}: {len(entries)} articles")
        time.sleep(1)  # be polite to Crossref
    write_opml(os.path.join(OUT_DIR, "feeds.opml"), base)
    open(os.path.join(OUT_DIR, ".nojekyll"), "w").close()
    if failures == len(JOURNALS):
        sys.exit(1)


if __name__ == "__main__":
    main()
