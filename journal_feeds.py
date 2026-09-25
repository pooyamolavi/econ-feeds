#!/usr/bin/env python3
"""Build Atom feeds of new journal articles from the Crossref API.

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

# ---------------------------------------------------------------------------
# Journals to track. Add or remove lines freely; any ISSN of the journal works.
# slug = file name of the feed (docs/<slug>.xml)
# ---------------------------------------------------------------------------
JOURNALS = [
    # slug,             name,                                   ISSN
    ("aer",             "American Economic Review",             "0002-8282"),
    ("aer-insights",    "AER: Insights",                        "2640-205X"),
    ("aej-macro",       "AEJ: Macroeconomics",                  "1945-7707"),
    ("qje",             "Quarterly Journal of Economics",       "0033-5533"),
    ("restud",          "Review of Economic Studies",           "0034-6527"),
    ("rfs",             "Review of Financial Studies",          "0893-9454"),
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


def write_atom(path, feed_id, name, entries):
    ns = "http://www.w3.org/2005/Atom"
    ET.register_namespace("", ns)
    feed = ET.Element(f"{{{ns}}}feed")
    ET.SubElement(feed, f"{{{ns}}}title").text = name
    ET.SubElement(feed, f"{{{ns}}}id").text = feed_id
    ET.SubElement(feed, f"{{{ns}}}updated").text = (
        entries[0]["updated"] if entries
        else datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    ET.SubElement(feed, f"{{{ns}}}link", href=feed_id, rel="self")
    for e in entries:
        entry = ET.SubElement(feed, f"{{{ns}}}entry")
        ET.SubElement(entry, f"{{{ns}}}title").text = e["title"]
        ET.SubElement(entry, f"{{{ns}}}id").text = e["id"]
        ET.SubElement(entry, f"{{{ns}}}link", href=e["id"])
        ET.SubElement(entry, f"{{{ns}}}updated").text = e["updated"]
        author = ET.SubElement(entry, f"{{{ns}}}author")
        ET.SubElement(author, f"{{{ns}}}name").text = e["authors"]
        summary = e["authors"] + (f"\n\n{e['abstract']}" if e["abstract"] else "")
        ET.SubElement(entry, f"{{{ns}}}summary").text = summary
    ET.ElementTree(feed).write(path, encoding="utf-8", xml_declaration=True)


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
    for slug, name, _ in JOURNALS:
        ET.SubElement(folder, "outline", type="rss", text=name, title=name,
                      xmlUrl=f"{base}{slug}.xml")
    ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    base = base_url()
    failures = 0
    for slug, name, issn in JOURNALS:
        try:
            items = fetch_works(issn)
        except RuntimeError as err:
            # Keep the previous feed file so the reader just sees no update.
            print(f"WARNING: {err}", file=sys.stderr)
            failures += 1
            continue
        entries = [e for e in (to_entry(i) for i in items) if e]
        write_atom(os.path.join(OUT_DIR, f"{slug}.xml"),
                   f"{base}{slug}.xml", name, entries)
        print(f"{name}: {len(entries)} articles")
        time.sleep(1)  # be polite to Crossref
    write_opml(os.path.join(OUT_DIR, "feeds.opml"), base)
    open(os.path.join(OUT_DIR, ".nojekyll"), "w").close()
    if failures == len(JOURNALS):
        sys.exit(1)


if __name__ == "__main__":
    main()
