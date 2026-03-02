import os
import json
import time
import asyncio
import hashlib
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import List, Dict, Any, Tuple

import requests
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright


# ----------------------------
# Config (from GitHub Secrets)
# ----------------------------
EMAIL_ADDRESS = os.getenv("EMAIL_ADDRESS", "").strip()
EMAIL_APP_PASSWORD = os.getenv("EMAIL_APP_PASSWORD", "").strip()
EMAIL_TO = os.getenv("EMAIL_TO", "").strip()

FORM_URL = os.getenv("FORM_URL", "").strip()  # Airtable public form URL
PRONTO_URL = os.getenv("PRONTO_URL", "https://www.prontohousingrentals.com/").strip()

UNITS_STATE_PATH = "units_state.json"
PRONTO_STATE_PATH = "pronto_state.json"


# ----------------------------
# Email helpers
# ----------------------------
def send_email(subject: str, body: str) -> None:
    """Send an email via Gmail SMTP (App Password)."""
    if not (EMAIL_ADDRESS and EMAIL_APP_PASSWORD and EMAIL_TO):
        print("[WARN] Email env vars missing; not sending email.")
        print("Subject:", subject)
        print(body)
        return

    msg = MIMEMultipart()
    msg["From"] = EMAIL_ADDRESS
    msg["To"] = EMAIL_TO
    msg["Subject"] = subject

    msg.attach(MIMEText(body, "plain"))

    with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as server:
        server.ehlo()
        server.starttls()
        server.login(EMAIL_ADDRESS, EMAIL_APP_PASSWORD)
        server.send_message(msg)

    print("[OK] Email sent:", subject)


# ----------------------------
# State helpers
# ----------------------------
def load_json(path: str, default: Any) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except Exception as e:
        print(f"[WARN] Failed to load {path}: {e}")
        return default


def save_json(path: str, data: Any) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def normalize_units(units: List[str]) -> List[str]:
    # Trim + remove empty + de-dupe while preserving order
    seen = set()
    out = []
    for u in units:
        u2 = " ".join(u.split()).strip()
        if not u2:
            continue
        if u2.lower() in seen:
            continue
        seen.add(u2.lower())
        out.append(u2)
    return out


def diff_lists(old: List[str], new: List[str]) -> Tuple[List[str], List[str]]:
    old_set = {x.lower(): x for x in old}
    new_set = {x.lower(): x for x in new}

    added = [new_set[k] for k in new_set.keys() if k not in old_set]
    removed = [old_set[k] for k in old_set.keys() if k not in new_set]

    # Keep stable-ish ordering
    added_sorted = [x for x in new if x.lower() in {a.lower() for a in added}]
    removed_sorted = [x for x in old if x.lower() in {r.lower() for r in removed}]
    return added_sorted, removed_sorted


# ----------------------------
# Airtable scraping (Playwright)
# ----------------------------
async def scrape_airtable_units() -> List[str]:
    """
    Opens the Airtable form and extracts the unit list behind the “Add unit” picker.
    We avoid wait_until="networkidle" because Airtable often keeps background requests running.
    """
    if not FORM_URL:
        raise RuntimeError("FORM_URL is missing. Add it to GitHub Secrets.")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()

        # Airtable can be slow on GitHub runners; domcontentloaded + selector wait is reliable
        await page.goto(FORM_URL, wait_until="domcontentloaded", timeout=90_000)
        await page.wait_for_selector("button:has-text('Add unit')", timeout=30_000)

        # Click the button to open the dropdown / picker
        await page.click("button:has-text('Add unit')")

        # Airtable may render options in a portal; we wait for common patterns.
        # We'll try multiple selectors to be resilient.
        options = []

        # A short wait for the options to render
        await page.wait_for_timeout(1000)

        # Candidate option selectors (Airtable changes these sometimes)
        candidate_selectors = [
            "[role='listbox'] [role='option']",
            "[role='option']",
            "div[aria-label*='Project'] [role='option']",
            "div[role='listbox'] div",
        ]

        for sel in candidate_selectors:
            els = await page.query_selector_all(sel)
            if not els:
                continue

            texts = []
            for el in els:
                try:
                    t = (await el.inner_text()) or ""
                except Exception:
                    t = ""
                t = " ".join(t.split()).strip()
                # Filter out obviously non-option junk
                if t and t.lower() not in {"add unit"} and len(t) > 2:
                    texts.append(t)

            texts = normalize_units(texts)

            # Heuristic: real option list will usually have multiple items
            if len(texts) >= 3:
                options = texts
                break

        await context.close()
        await browser.close()

        if not options:
            # Last-ditch: capture the page content for debugging if needed
            raise RuntimeError("Could not detect Airtable unit options after clicking 'Add unit'.")

        return options


async def scrape_airtable_units_with_retries(retries: int = 3, delay_seconds: int = 5) -> List[str]:
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            print(f"[Airtable] Attempt {attempt}/{retries}...")
            units = await scrape_airtable_units()
            print(f"[Airtable] Found {len(units)} unit options.")
            return units
        except Exception as e:
            last_err = e
            print(f"[Airtable] Attempt {attempt} failed: {e}")
            if attempt < retries:
                await asyncio.sleep(delay_seconds)
    raise last_err


# ----------------------------
# Pronto scraping (Requests/BS4)
# ----------------------------
def scrape_pronto_homepage() -> List[str]:
    """
    Scrape ProntoHousing homepage and extract listing-ish text blocks.
    Because sites vary, we compute a stable signature from visible text chunks.
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome Safari"
    }
    resp = requests.get(PRONTO_URL, headers=headers, timeout=30)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")

    # Collect likely listing cards/blocks by scanning headings + strong tags + links
    chunks = []

    # Links often contain the listing titles
    for a in soup.select("a"):
        t = a.get_text(" ", strip=True)
        if t and len(t) >= 6:
            chunks.append(t)

    # Headings
    for tag in soup.select("h1,h2,h3,h4,strong"):
        t = tag.get_text(" ", strip=True)
        if t and len(t) >= 6:
            chunks.append(t)

    chunks = normalize_units(chunks)

    # Optional: reduce noise by keeping only chunks that look like rentals
    keywords = ("apt", "apartment", "bed", "br", "$", "rent", "studio", "housing")
    filtered = [c for c in chunks if any(k in c.lower() for k in keywords)]

    # If filtering becomes too aggressive, fall back to chunks
    result = filtered if len(filtered) >= 5 else chunks
    print(f"[Pronto] Extracted {len(result)} text chunks.")
    return result


def signature(items: List[str]) -> str:
    joined = "\n".join(items).encode("utf-8")
    return hashlib.sha256(joined).hexdigest()


# ----------------------------
# Main monitor logic
# ----------------------------
async def main() -> None:
    alerts = []

    # --- Airtable ---
    airtable_prev = load_json(UNITS_STATE_PATH, {"units": []})
    prev_units = normalize_units(airtable_prev.get("units", []))

    curr_units = await scrape_airtable_units_with_retries(retries=3, delay_seconds=6)
    curr_units = normalize_units(curr_units)

    added, removed = diff_lists(prev_units, curr_units)
    airtable_changed = bool(added or removed)

    if airtable_changed:
        alerts.append("=== Airtable Form (Reside NY Rerentals) ===")
        if added:
            alerts.append("ADDED:")
            alerts.extend([f"  + {x}" for x in added])
        if removed:
            alerts.append("REMOVED (could mean taken):")
            alerts.extend([f"  - {x}" for x in removed])
        alerts.append("")

        save_json(UNITS_STATE_PATH, {"units": curr_units, "updated_at": time.time()})
        print("[Airtable] State updated.")
    else:
        print("[Airtable] No change detected.")

    # --- Pronto ---
    pronto_prev = load_json(PRONTO_STATE_PATH, {"sig": "", "items": []})
    prev_sig = pronto_prev.get("sig", "")
    prev_items = pronto_prev.get("items", [])

    curr_items = scrape_pronto_homepage()
    curr_sig = signature(curr_items)

    pronto_changed = (curr_sig != prev_sig)

    if pronto_changed:
        alerts.append("=== ProntoHousing Homepage ===")
        alerts.append("Homepage content signature changed (likely new/removed listings).")
        alerts.append(f"URL: {PRONTO_URL}")
        alerts.append("")
        # You can include top items for quick scanning:
        alerts.append("Top items (snapshot):")
        for x in curr_items[:25]:
            alerts.append(f"  • {x}")
        alerts.append("")

        save_json(PRONTO_STATE_PATH, {"sig": curr_sig, "items": curr_items, "updated_at": time.time()})
        print("[Pronto] State updated.")
    else:
        print("[Pronto] No change detected.")

    # --- Email if anything changed ---
    if alerts:
        subject = "🚨 Apartment Listings Updated"
        body = "\n".join(alerts)
        send_email(subject, body)
    else:
        print("[OK] No updates. No email sent.")


if __name__ == "__main__":
    asyncio.run(main())
