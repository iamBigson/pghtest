#!/usr/bin/env python3
"""
PageHuntr lead engine.

Searches "<keyword> in <state>" with Serper, keeps only domains whose mail is
hosted on the chosen provider (Office 365 or Google Workspace, found through
MX records), finds each site's contact page, sends the results to Telegram,
and saves the run to results/ so the dashboard can show history.

Environment variables:
  SERPER_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID   (GitHub secrets)
  KEYWORD, STATE, PROVIDER                               (workflow inputs)
  PROVIDER is one of: office365, google, random
"""

import os
import sys
import csv
import json
import random
import re
import time
from datetime import datetime, timezone
from urllib.parse import urlparse, urljoin

import dns.resolver
import requests

SEEN_FILE = "seen_domains.json"
RESULTS_DIR = "results"
TARGET_NEW = 20
MAX_PAGES = 20
REQUEST_TIMEOUT = 10
DNS_TIMEOUT = 5

CONTACT_PATTERNS = [
    "contact", "contact-us", "contactus", "get-in-touch", "getintouch",
    "book", "booking", "inquire", "inquiry", "enquiry", "request-quote",
    "quote", "reach-us", "reach-out", "connect",
]

PROVIDERS = {
    "office365": {"label": "Office 365", "markers": ["outlook.com"]},
    "google": {"label": "Google Workspace", "markers": ["google.com", "googlemail.com"]},
}


class SerperError(Exception):
    def __init__(self, code):
        super().__init__(f"Serper returned {code}")
        self.code = code


def get_env(name):
    val = os.environ.get(name)
    if not val:
        print(f"ERROR: missing required environment variable {name}", file=sys.stderr)
        sys.exit(1)
    return val


def load_seen():
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE, "r") as f:
            return set(json.load(f))
    return set()


def save_seen(seen_set):
    with open(SEEN_FILE, "w") as f:
        json.dump(sorted(seen_set), f, indent=2)


def normalize_domain(url):
    try:
        netloc = urlparse(url).netloc.lower()
        return netloc[4:] if netloc.startswith("www.") else netloc
    except Exception:
        return None


def mail_provider(domain):
    """Return 'office365', 'google', or None, based on the domain's MX records."""
    try:
        resolver = dns.resolver.Resolver()
        resolver.timeout = DNS_TIMEOUT
        resolver.lifetime = DNS_TIMEOUT
        answers = resolver.resolve(domain, "MX")
        exchanges = [str(r.exchange).rstrip(".").lower() for r in answers]
    except Exception:
        return None
    for key, info in PROVIDERS.items():
        if any(m in ex for ex in exchanges for m in info["markers"]):
            return key
    return None


def serper_search(api_key, query, page):
    resp = requests.post(
        "https://google.serper.dev/search",
        headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
        json={"q": query, "page": page},
        timeout=REQUEST_TIMEOUT,
    )
    if resp.status_code in (401, 403, 429):
        raise SerperError(resp.status_code)
    resp.raise_for_status()
    return resp.json().get("organic", [])


def find_contact_page(homepage_url):
    """Scan the homepage's links for a contact-like page. Falls back to the homepage."""
    try:
        resp = requests.get(
            homepage_url,
            timeout=REQUEST_TIMEOUT,
            headers={"User-Agent": "Mozilla/5.0 (compatible; LeadFinderBot/1.0)"},
        )
        resp.raise_for_status()
        html = resp.text
    except Exception:
        return homepage_url, False

    links = re.findall(
        r'<a\s+[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>',
        html, re.IGNORECASE | re.DOTALL,
    )
    for href, text in links:
        clean_text = re.sub(r"<[^>]+>", "", text).strip().lower()
        href_lower = href.lower()
        for pattern in CONTACT_PATTERNS:
            if pattern in href_lower or pattern in clean_text:
                full_url = urljoin(homepage_url, href)
                if full_url.startswith("http"):
                    return full_url, True
    return homepage_url, False


def send_telegram(bot_token, chat_id, message, parse_mode=None):
    """Returns True if Telegram accepted the message."""
    payload = {"chat_id": chat_id, "text": message, "disable_web_page_preview": True}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json=payload,
            timeout=REQUEST_TIMEOUT,
        )
    except Exception as e:
        # Print only the error type: the full message would contain the bot token.
        print(f"WARNING: Telegram send failed ({type(e).__name__})", file=sys.stderr)
        return False
    if resp.status_code != 200:
        print(f"WARNING: Telegram send failed ({resp.status_code}): {resp.text}", file=sys.stderr)
        return False
    return True


def escape_html(text):
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def build_messages(query, label, leads):
    header = (
        f"🎯 <b>{escape_html(query)}</b>\n"
        f"✅ {len(leads)} new {escape_html(label)} lead(s)\n"
        f"{'─' * 24}\n"
    )
    blocks = []
    for i, (domain, contact_url, found) in enumerate(leads, start=1):
        c_label = "C page" if found else "C page (not found, homepage below)"
        blocks.append(
            f"<b>{i}. {escape_html(domain)}</b>\n"
            f"🌐 Domain: {escape_html(domain)}\n"
            f"📩 {c_label}: {escape_html(contact_url)}"
        )
    message = header + "\n\n".join(blocks)
    if len(message) <= 4000:
        return [message]
    messages, chunk = [header], ""
    for block in blocks:
        if len(chunk) + len(block) > 3800:
            messages.append(chunk)
            chunk = ""
        chunk += block + "\n\n"
    if chunk:
        messages.append(chunk)
    return messages


def save_run(meta, leads):
    os.makedirs(RESULTS_DIR, exist_ok=True)
    base = os.path.join(RESULTS_DIR, meta["id"])
    meta = dict(meta, leads=[
        {"domain": d, "contact_url": u, "contact_found": f} for d, u, f in leads
    ])
    with open(base + ".json", "w") as f:
        json.dump(meta, f, indent=2)
    with open(base + ".csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["domain", "contact_url", "contact_found"])
        for d, u, found in leads:
            w.writerow([d, u, "yes" if found else "no"])


def main():
    keyword = (os.environ.get("KEYWORD") or (sys.argv[1] if len(sys.argv) > 1 else "")).strip()
    state = (os.environ.get("STATE") or (sys.argv[2] if len(sys.argv) > 2 else "")).strip()
    choice = (os.environ.get("PROVIDER") or (sys.argv[3] if len(sys.argv) > 3 else "office365")).strip().lower()

    if not keyword or not state:
        print("ERROR: keyword and state are required", file=sys.stderr)
        sys.exit(1)
    if choice not in ("office365", "google", "random"):
        print("ERROR: provider must be office365, google or random", file=sys.stderr)
        sys.exit(1)

    provider = random.choice(list(PROVIDERS)) if choice == "random" else choice
    label = PROVIDERS[provider]["label"]
    query = f"{keyword} in {state}"

    serper_key = get_env("SERPER_API_KEY")
    tg_token = get_env("TELEGRAM_BOT_TOKEN")
    tg_chat = get_env("TELEGRAM_CHAT_ID")

    started = datetime.now(timezone.utc)
    run_id = started.strftime("%Y%m%d-%H%M%S") + "-" + os.environ.get("GITHUB_RUN_ID", "local")

    seen = load_seen()
    leads = []
    checked_this_session = set()
    skipped = 0
    status, error = "done", None

    print(f"Query: {query}")
    print(f"Provider: {label} (requested: {choice})")
    print(f"Delivered domains on file: {len(seen)}")

    page = 1
    while len(leads) < TARGET_NEW and page <= MAX_PAGES:
        try:
            results = serper_search(serper_key, query, page)
        except SerperError as e:
            status, error = "serper_error", f"Serper returned {e.code}"
            print(f"Serper error {e.code} on page {page}", file=sys.stderr)
            break
        except Exception as e:
            status, error = "search_error", f"Search failed ({type(e).__name__})"
            print(f"Search error on page {page}: {type(e).__name__}", file=sys.stderr)
            break

        if not results:
            print(f"No more results at page {page}, stopping search.")
            break

        for r in results:
            domain = normalize_domain(r.get("link") or "")
            if not domain or domain in seen or domain in checked_this_session:
                continue
            checked_this_session.add(domain)

            if mail_provider(domain) != provider:
                skipped += 1
                print(f"  - skipped (not {label}): {domain}")
                continue

            contact_url, found = find_contact_page(f"https://{domain}")
            leads.append((domain, contact_url, found))
            print(f"  + new {label} domain: {domain} (contact page found: {found})")
            if len(leads) >= TARGET_NEW:
                break

        page += 1
        time.sleep(0.5)

    # Only delivered domains count as seen, so a Google domain skipped in an
    # Office 365 run can still be found in a later Google run.
    seen.update(d for d, _, _ in leads)
    save_seen(seen)
    print(f"Skipped (not {label} or undetermined): {skipped}")

    telegram_ok = True
    if leads:
        for msg in build_messages(query, label, leads):
            telegram_ok &= send_telegram(tg_token, tg_chat, msg, parse_mode="HTML")
        if len(leads) < TARGET_NEW and status == "done":
            telegram_ok &= send_telegram(
                tg_token, tg_chat,
                f"⚠️ Only found {len(leads)}/{TARGET_NEW} new {label} domains "
                f"before hitting the search cap ({MAX_PAGES} pages).",
            )
    elif status == "done":
        telegram_ok &= send_telegram(
            tg_token, tg_chat,
            f"🔍 <b>{escape_html(query)}</b>\n\nNo new {escape_html(label)} domains found "
            f"({skipped} checked and skipped).",
            parse_mode="HTML",
        )

    if status == "serper_error":
        telegram_ok &= send_telegram(
            tg_token, tg_chat,
            "🚨 Your Serper key was rejected or is out of credits "
            f"({error}). Renew or replace it in PageHuntr, then run again.",
        )
    elif status == "search_error":
        telegram_ok &= send_telegram(
            tg_token, tg_chat, f"⚠️ The search stopped early: {error}. Try again in a few minutes."
        )

    if not telegram_ok and status == "done":
        status, error = "telegram_error", "Telegram rejected the message. Check your bot token and chat ID."

    finished = datetime.now(timezone.utc)
    save_run({
        "id": run_id,
        "query": query,
        "keyword": keyword,
        "state": state,
        "provider": provider,
        "provider_requested": choice,
        "provider_label": label,
        "target": TARGET_NEW,
        "found": len(leads),
        "checked": len(checked_this_session),
        "skipped": skipped,
        "status": status,
        "error": error,
        "telegram_ok": telegram_ok,
        "started_at": started.isoformat(),
        "finished_at": finished.isoformat(),
    }, leads)

    print(f"Done with status '{status}'. Found {len(leads)}/{TARGET_NEW}.")
    if status == "serper_error":
        sys.exit(2)
    if status == "telegram_error":
        sys.exit(3)


if __name__ == "__main__":
    main()
