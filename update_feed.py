#!/usr/bin/env python3
"""
Refresh updates.json from the public RSS/Atom feeds listed in feeds.json.

* Uses only the Python standard library (Python 3.8 or newer).
* Keeps headlines and links only, credited to the source. Nothing else is copied.
* A source can be given a "group". The page shows the group "ap" (Andhra Pradesh) in its
  own list; everything else goes in the general list.
* A source can choose which date to show: "date_field": "updated" (when the record was last
  updated) or "published" (the original date). The default takes whichever the feed offers.
* Items stay on the list until they are older than the source's age limit, so a source that
  publishes only now and then still fills its list.
* If every source fails, the existing updates.json is left untouched.
* If one source fails, its previous items are kept until they age out.

Run:
    python update_feed.py            normal run: fetch, filter, write updates.json
    python update_feed.py --check    test the sources and print a report; writes nothing
"""

import argparse
import datetime as dt
import html
import json
import re
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
USER_AGENT = "Mozilla/5.0 (compatible; juristdvr-updates/1.0; +https://juristdvr.com/)"
MAX_BYTES = 3_000_000
TIMEOUT = 25
DEFAULT_GROUP = "general"
EPOCH = dt.datetime.min.replace(tzinfo=dt.timezone.utc)
TAG_RE = re.compile(r"<[^>]+>")
SPACE_RE = re.compile(r"\s+")


# --------------------------------------------------------------------------
# Fetching and parsing
# --------------------------------------------------------------------------

def local(tag):
    """Tag name without its XML namespace."""
    return tag.rsplit("}", 1)[-1]


def fetch(url):
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, text/xml;q=0.9, */*;q=0.5",
        },
    )
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        data = response.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError("feed is larger than 3 MB")
    return data


def clean(text, limit=220):
    """Strip tags, decode entities, collapse spaces and shorten."""
    text = html.unescape(TAG_RE.sub(" ", text or ""))
    text = SPACE_RE.sub(" ", text).strip()
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "\u2026"
    return text


def child_text(element, *names):
    for child in element:
        if local(child.tag) in names:
            return "".join(child.itertext()).strip()
    return ""


def entry_link(element):
    fallback = ""
    for child in element:
        if local(child.tag) != "link":
            continue
        href = child.get("href")
        if href:
            if child.get("rel", "alternate") == "alternate":
                return href.strip()
            fallback = fallback or href.strip()
        elif child.text and child.text.strip():
            return child.text.strip()
    if fallback:
        return fallback
    guid = child_text(element, "guid")
    return guid if guid.lower().startswith("http") else ""


def parse_date(value):
    value = (value or "").strip()
    if not value:
        return None
    parsed = None
    try:
        parsed = parsedate_to_datetime(value)  # RFC 822 style, used by RSS
    except (TypeError, ValueError, IndexError):
        parsed = None
    if parsed is None:
        try:
            parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))  # ISO 8601, used by Atom
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def choose_date(dates, field="auto"):
    """Pick one of an entry's dates. 'updated' prefers the last-updated date, 'published' the original."""
    if field == "updated":
        order = ("updated", "pubDate", "published", "date")
    elif field == "published":
        order = ("pubDate", "published", "date", "updated")
    else:
        order = ("pubDate", "published", "updated", "date")
    for name in order:
        if dates.get(name):
            return dates[name]
    return None


def parse_feed(data, base_url=""):
    """Return a list of {title, url, dates, match_text} from RSS 2.0, RSS 1.0 or Atom."""
    if b"<!ENTITY" in data:
        raise ValueError("feed declares XML entities; skipped for safety")
    root = ET.fromstring(data)
    entries = []
    for element in root.iter():
        if local(element.tag) not in ("item", "entry"):
            continue
        title = clean(child_text(element, "title"))
        link = entry_link(element)
        if not title or not link:
            continue
        link = urllib.parse.urljoin(base_url, link)
        if urllib.parse.urlparse(link).scheme not in ("http", "https"):
            continue
        dates = {name: parse_date(child_text(element, name)) for name in ("pubDate", "published", "updated", "date")}
        summary = clean(child_text(element, "description", "summary", "encoded"), limit=2000)
        entries.append({"title": title, "url": link, "dates": dates, "match_text": f"{title} {summary}"})
    return entries


# --------------------------------------------------------------------------
# Filtering
# --------------------------------------------------------------------------

def compile_keywords(words):
    """'pension' matches the whole word; 'pension*' matches any word starting with it."""
    patterns = []
    for word in words or []:
        word = str(word).strip().lower()
        if not word:
            continue
        if word.endswith("*"):
            patterns.append(re.compile(r"\b" + re.escape(word[:-1])))
        else:
            patterns.append(re.compile(r"\b" + re.escape(word) + r"\b"))
    return patterns


def matches(text, include, exclude):
    text = text.lower()
    if exclude and any(p.search(text) for p in exclude):
        return False
    return (not include) or any(p.search(text) for p in include)


def parse_iso_day(value):
    try:
        return dt.datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=dt.timezone.utc)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------
# Collecting
# --------------------------------------------------------------------------

def collect(config, previous, now, log):
    """Fetch every enabled source. Returns (items, number_of_sources_that_responded)."""
    default_age = int(config.get("max_age_days", 120))
    default_limit = int(config.get("per_source_limit", 6))
    per_group = int(config.get("max_items", 8))
    global_exclude = compile_keywords(config.get("exclude"))
    sources = config.get("sources", [])

    # Settings for each source, and the order in which groups appear.
    settings, group_order = {}, []
    for source in sources:
        name = source.get("name") or source.get("url") or "Unnamed source"
        group = source.get("group") or DEFAULT_GROUP
        try:
            max_age = dt.timedelta(days=int(source.get("max_age_days", default_age)))
            limit = int(source.get("per_source_limit", default_limit))
        except (TypeError, ValueError):
            max_age, limit = dt.timedelta(days=default_age), default_limit
            log(f"NOTE     {name}: 'max_age_days' or 'per_source_limit' is not a number; defaults used")
        settings[name] = (group, max_age, limit)
        if group not in group_order:
            group_order.append(group)

    collected, responded, failed, active = [], 0, [], set()

    for source in sources:
        name = source.get("name") or source.get("url") or "Unnamed source"
        group, max_age, limit = settings[name]
        if not source.get("enabled", True):
            log(f"SKIPPED  {name} (switched off)")
            continue
        url = source.get("url", "")
        active.add(name)
        if urllib.parse.urlparse(url).scheme not in ("http", "https"):
            log(f"FAILED   {name}: the address must start with http:// or https://")
            failed.append(name)
            continue
        try:
            entries = parse_feed(fetch(url), url)
        except Exception as exc:  # one broken source must never stop the others
            log(f"FAILED   {name}: {type(exc).__name__}: {exc}")
            failed.append(name)
            continue

        include = compile_keywords(source.get("keywords"))
        exclude = global_exclude + compile_keywords(source.get("exclude"))
        field = source.get("date_field", "auto")
        kept = []
        for entry in entries:
            date = choose_date(entry["dates"], field)
            if date and now - date > max_age:
                continue
            if date and date - now > dt.timedelta(days=2):
                continue  # dated in the future: ignore
            if not matches(entry["match_text"], include, exclude):
                continue
            kept.append((date, entry))
        kept.sort(key=lambda pair: pair[0] or EPOCH, reverse=True)
        kept = kept[:limit]
        responded += 1
        log(f"OK       {name} [{group}]: {len(entries)} in the feed, {len(kept)} kept")
        for date, entry in kept:
            collected.append({
                "title": entry["title"],
                "url": entry["url"],
                "source": name,
                "group": group,
                "date": date.date().isoformat() if date else None,
            })

    # Keep earlier items until they age out. Items with no date are kept only while their source is failing.
    # Items from a source that was removed or switched off are dropped.
    for item in previous:
        name = item.get("source")
        if name not in active or name not in settings:
            continue
        if not (isinstance(item.get("url"), str) and isinstance(item.get("title"), str)):
            continue
        group, max_age, _ = settings[name]
        day = parse_iso_day(item.get("date"))
        if day is None:
            if name not in failed:
                continue
        elif now - day > max_age:
            continue
        collected.append({"title": item["title"], "url": item["url"], "source": name,
                          "group": group, "date": item.get("date")})

    seen, unique = set(), []
    for item in collected:              # fresh items come first, so they win over older copies
        if item["url"] not in seen:
            seen.add(item["url"])
            unique.append(item)

    # Apply each source's own limit to fresh and earlier items together.
    by_source = {}
    for item in unique:
        by_source.setdefault(item["source"], []).append(item)
    unique = []
    for name, items in by_source.items():
        items.sort(key=lambda i: parse_iso_day(i.get("date")) or EPOCH, reverse=True)
        unique.extend(items[: settings[name][2]])

    # Newest first inside each group; groups in the order they appear in feeds.json.
    groups = {}
    for item in unique:
        groups.setdefault(item["group"], []).append(item)
    ordered = [g for g in group_order if g in groups] + [g for g in groups if g not in group_order]
    result = []
    for group in ordered:
        items = sorted(groups[group], key=lambda i: parse_iso_day(i.get("date")) or EPOCH, reverse=True)
        result.extend(items[:per_group])
    return result, responded


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Refresh updates.json from public feeds.")
    parser.add_argument("--config", default=str(HERE / "feeds.json"), help="feed list (default: feeds.json)")
    parser.add_argument("--out", default=str(HERE / "updates.json"), help="output file (default: updates.json)")
    parser.add_argument("--check", action="store_true", help="test the sources and print a report; write nothing")
    args = parser.parse_args()

    try:
        config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Cannot read {args.config}: {exc}", file=sys.stderr)
        return 2

    out = Path(args.out)
    previous = []
    if out.exists():
        try:
            previous = json.loads(out.read_text(encoding="utf-8")).get("items", [])
        except (OSError, json.JSONDecodeError, AttributeError):
            previous = []

    now = dt.datetime.now(dt.timezone.utc)
    items, responded = collect(config, previous, now, print)

    if responded == 0:
        print("No source responded (or none is switched on). The existing updates.json was left unchanged.")
        return 0

    groups = sorted({i["group"] for i in items})
    summary = f"{len(items)} item(s) in {len(groups)} group(s) ({', '.join(groups) or 'none'})"

    if args.check:
        print(f"Check only: {summary} would be written. Nothing was saved.")
        return 0

    payload = {"generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "items": items}
    temporary = out.with_name(out.name + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(out)
    print(f"Wrote {summary} to {out.name}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
