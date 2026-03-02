import os
import json
import asyncio
import smtplib
from email.message import EmailMessage
from datetime import datetime

import requests
from bs4 import BeautifulSoup

STATE_AIRTABLE = "units_state.json"
STATE_PRONTO = "pronto_state.json"

FORM_URL = os.getenv("FORM_URL", "https://airtable.com/appsseXTOVx59HC0W/pagcVengefPFQvMZC/form")
PRONTO_URL = os.getenv("PRONTO_URL", "https://www.prontohousingrentals.com/")

EMAIL_ADDRESS = os.getenv("EMAIL_ADDRESS")
EMAIL_APP_PASSWORD = os.getenv("EMAIL_APP_PASSWORD")
EMAIL_TO = os.getenv("EMAIL_TO")


def send_email(subject, body):
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = EMAIL_ADDRESS
    msg["To"] = EMAIL_TO
    msg.set_content(body)

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(EMAIL_ADDRESS, EMAIL_APP_PASSWORD)
        smtp.send_message(msg)


def load_state(path):
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_state(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def diff(prev, curr):
    prev_set, curr_set = set(prev), set(curr)
    added = sorted(curr_set - prev_set)
    removed = sorted(prev_set - curr_set)
    return added, removed


# ---------------- Airtable (Playwright) ----------------
async def scrape_airtable_units():
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.goto(FORM_URL, wait_until="networkidle", timeout=60000)

        # Open dropdown/picker
        await page.get_by_text("Add unit", exact=False).click(timeout=30000)
        await page.wait_for_selector('[role="option"]', timeout=30000)

        options = await page.locator('[role="option"]').all_inner_texts()
        units = sorted({o.strip() for o in options if o and o.strip()})

        await browser.close()
        return units


# ---------------- Pronto (Requests + BeautifulSoup) ----------------
def scrape_pronto_homepage():
    r = requests.get(PRONTO_URL, timeout=30)
    r.raise_for_status()

    soup = BeautifulSoup(r.text, "html.parser")

    # Pronto page has “Available Units” section and property headings (often h3). :contentReference[oaicite:4]{index=4}
    # We'll collect headings + their first “Apply/Join Waitlist” link that follows.
    lines = []

    # Grab all h3 titles (property names appear as ### headings in page text). :contentReference[oaicite:5]{index=5}
    for h in soup.find_all(["h2", "h3"]):
        title = h.get_text(" ", strip=True)
        if not title:
            continue

        # Filter out obvious non-property headings
        if title.lower() in {"available units", "affordable housing availabilities"}:
            continue

        # Find the next link after this heading that looks like Apply/Join
        link = None
        nxt = h
        for _ in range(0, 60):  # scan forward a bit
            nxt = nxt.find_next()
            if nxt is None:
                break
            if nxt.name == "a":
                txt = nxt.get_text(" ", strip=True).lower()
                href = nxt.get("href", "")
                if "apply" in txt or "join waitlist" in txt or "waitlist" in txt:
                    link = href
                    break

        if link:
            lines.append(f"{title} | {link}")
        else:
            # Still track the title in case they change structure
            lines.append(f"{title}")

    # Deduplicate and keep stable ordering
    return sorted(set(lines))


async def main():
    email_chunks = []
    changed_anything = False

    # ---- Airtable compare ----
    prev_air = load_state(STATE_AIRTABLE)
    curr_air = await scrape_airtable_units()

    if not prev_air:
        save_state(STATE_AIRTABLE, curr_air)
        email_chunks.append(f"Airtable baseline saved ({len(curr_air)} units).")
    else:
        added, removed = diff(prev_air, curr_air)
        if added or removed:
            changed_anything = True
            chunk = [f"Airtable units changed at {datetime.now()}",
                     f"Total now: {len(curr_air)}"]
            if added:
                chunk.append("\nADDED:\n" + "\n".join(f"• {x}" for x in added))
            if removed:
                chunk.append("\nREMOVED:\n" + "\n".join(f"• {x}" for x in removed))
            email_chunks.append("\n".join(chunk))
            save_state(STATE_AIRTABLE, curr_air)

    # ---- Pronto compare ----
    prev_pro = load_state(STATE_PRONTO)
    curr_pro = scrape_pronto_homepage()

    if not prev_pro:
        save_state(STATE_PRONTO, curr_pro)
        email_chunks.append(f"Pronto baseline saved ({len(curr_pro)} items).")
    else:
        added, removed = diff(prev_pro, curr_pro)
        if added or removed:
            changed_anything = True
            chunk = [f"Pronto homepage changed at {datetime.now()}",
                     f"Total now: {len(curr_pro)}"]
            if added:
                chunk.append("\nADDED/NEW:\n" + "\n".join(f"• {x}" for x in added))
            if removed:
                chunk.append("\nREMOVED:\n" + "\n".join(f"• {x}" for x in removed))
            email_chunks.append("\n".join(chunk))
            save_state(STATE_PRONTO, curr_pro)

    # ---- Email ----
    if changed_anything:
        send_email("🚨 Apartment listings updated", "\n\n---\n\n".join(email_chunks))
        print("Email sent.")
    else:
        print("No changes detected.")


if __name__ == "__main__":
    asyncio.run(main())
