"""
Web OTP login with Playwright in one browser session on this machine (a HyperExecute VM or your laptop), with
Chromium started here (no LambdaTest Playwright grid). Standalone, one process.

  Step 1  request_otp         open the portal, sign in with the email, wait for the OTP page; the page stays open.
  Step 2  get_otp             OTP env var, else poll OTP_COMMAND (a command that prints only the code, e.g. a
                              curl to an endpoint that reads it from your database), else type it when asked.
  Step 3  verify_otp          on the SAME page: enter the OTP, capture the POST /api/esim/v2/verify-email response.
  Step 4  check_access_token  fail if verify-email returned no data.access_token.

The portal keeps its OTP stage only in the open page's memory, so steps 1 and 3 must share one browser page:
a new browser session with storageState lands back on the email form.

Run:
    pip install playwright && python -m playwright install chromium
    export LOGIN_EMAIL=...   (and OTP or OTP_COMMAND, or type the OTP when asked)
    python lambdatest_playwright_storage_state_two_steps.py
Optional: WEB_URL (default below), HEADLESS (default "true"), OTP_WAIT_SECONDS (default 300).
Screenshots go to ./out/.
"""
import json
import os
import re
import subprocess
import sys
import time

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

BASE = os.environ.get("WEB_URL", "https://www.montyesim.com").rstrip("/")
EMAIL = os.environ["LOGIN_EMAIL"]
OUT = os.path.abspath("out")
HEADLESS = os.environ.get("HEADLESS", "true").lower() != "false"


def accept_cookies(page, timeout=10000):
    """The cookie banner shows a moment after the page loads and covers the form."""
    try:
        page.locator("#rcc-confirm-button").first.wait_for(state="visible", timeout=timeout)
    except PlaywrightTimeoutError:
        return
    page.evaluate("document.querySelector('#rcc-confirm-button')?.click()")  # the chat launcher can sit over it


def request_otp(page):
    """Step 1: request the OTP; the page is left on the OTP screen for step 3."""
    page.goto(BASE + "/")
    accept_cookies(page)
    page.locator("a[href='/sign-in']:visible").first.click()
    page.locator("#signinEmail:visible").first.fill(EMAIL)
    page.locator("input[type='checkbox']:visible").first.click()  # Terms & Conditions
    page.locator("xpath=//button[normalize-space()='Sign in']").locator("visible=true").first.click()
    page.locator("#pin0:visible").first.wait_for(timeout=45000)
    page.evaluate("document.querySelector(\"[role='dialog'] button.times-button\")?.click()")  # 'Email Sent' popup
    page.screenshot(path=os.path.join(OUT, "otp_page.png"))
    print(f"step 1: OTP page open ({page.url})", flush=True)


def get_otp():
    """Step 2: the OTP env var, else the code OTP_COMMAND prints (polled every 5s), else ask on the terminal."""
    if os.environ.get("OTP", "").strip():
        return os.environ["OTP"].strip()
    command = os.environ.get("OTP_COMMAND")
    if not command:
        if not sys.stdin.isatty():
            sys.exit("step 2: no terminal to type the OTP: set OTP, or OTP_COMMAND (a command that prints the OTP)")
        return input("step 2: enter the OTP from the email: ").strip()
    deadline = time.monotonic() + int(os.environ.get("OTP_WAIT_SECONDS", "300"))
    while True:
        result = subprocess.run(command, shell=True, capture_output=True, text=True)
        code = result.stdout.strip()
        if result.returncode == 0 and re.fullmatch(r"\d{4,8}", code):
            print("step 2: OTP received", flush=True)
            return code
        if time.monotonic() >= deadline:
            sys.exit(f"step 2: no OTP from OTP_COMMAND in time (last exit {result.returncode}: "
                     f"{(result.stderr or result.stdout).strip()[:300]})")
        time.sleep(5)


def verify_otp(page, otp):
    """Step 3: enter the OTP on the same, still-open OTP page; return the verify-email access token."""
    page.locator("#pin0:visible").first.wait_for(timeout=10000)
    page.locator("#pin0:visible").first.click()
    page.keyboard.type(otp)  # each digit moves to the next box
    with page.expect_response(lambda r: "/api/esim/v2/verify-email" in r.url
                              and r.request.method == "POST", timeout=30000) as verify:
        page.locator("xpath=//button[normalize-space()='Verify']").locator("visible=true").first.click()
    response = verify.value
    try:
        body = response.json()
    except ValueError:
        body = None
    token = ((body or {}).get("data") or {}).get("access_token")
    assert token, (f"step 4: no access token: verify-email answered {response.status} "
                   f"{json.dumps(body)[:300] if body is not None else response.text()[:300]}")

    page.locator("#pin0").first.wait_for(state="hidden", timeout=20000)
    page.goto(BASE + "/")
    page.locator("xpath=//button[normalize-space()='USD']").locator("visible=true").first.wait_for(timeout=20000)
    page.locator("a[href='/sign-in']:visible").first.wait_for(state="hidden", timeout=15000)  # gone once signed in
    page.screenshot(path=os.path.join(OUT, "signed_in.png"))
    return token


def main():
    os.makedirs(OUT, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=HEADLESS)
        try:
            page = browser.new_page()
            request_otp(page)
            token = verify_otp(page, get_otp())
        finally:
            browser.close()
    assert token, "step 4: the access token is missing after login"
    print("step 4: access token captured in the same browser session — PASS")


if __name__ == "__main__":
    main()
