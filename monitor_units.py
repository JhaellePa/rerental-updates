import os
import json
import asyncio
import smtplib
from email.message import EmailMessage
from datetime import datetime

FORM_URL = os.getenv("FORM_URL", "https://airtable.com/appsseXTOVx59HC0W/pagcVengefPFQvMZC/form")
STATE_FILE = "units_state.json"

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

def load_prev():
    if not os.path.exists(STATE_FILE):
        return []
    with open(STATE_FILE, "r") as f:
        return json.load(f)

def save_curr(units):
    with open(STATE_FILE, "w") as f:
        json.dump(units, f, indent=2)

async def scrape_units():
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.goto(FORM_URL, wait_until="networkidle")

        await page.get_by_text("Add unit").click()
        await page.wait_for_selector('[role="option"]')

        options = await page.locator('[role="option"]').all_inner_texts()
        units = sorted(set(o.strip() for o in options if o.strip()))

        await browser.close()
        return units

async def main():
    prev = load_prev()
    curr = await scrape_units()

    if not prev:
        save_curr(curr)
        print("Baseline saved.")
        return

    added = sorted(set(curr) - set(prev))
    removed = sorted(set(prev) - set(curr))

    if added or removed:
        body = f"Units changed at {datetime.now()}\n\n"

        if added:
            body += "ADDED:\n" + "\n".join(added) + "\n\n"
        if removed:
            body += "REMOVED:\n" + "\n".join(removed)

        send_email("🚨 Apartment Units Updated", body)
        save_curr(curr)
        print("Email sent.")
    else:
        print("No change detected.")

if __name__ == "__main__":
    asyncio.run(main())
