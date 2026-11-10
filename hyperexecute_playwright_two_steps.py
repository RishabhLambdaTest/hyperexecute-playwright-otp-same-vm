"""
Web OTP login with Playwright split over two steps that share ONE live browser on the same machine (the
HyperExecute VM, or your laptop), with Chromium started on that machine (no LambdaTest Playwright grid).

Why one live browser: the portal keeps its OTP stage only in the open page's memory, on /sign-in. A new browser
session never gets it back, even with every cookie and localStorage item restored and on the same machine
(tried: 7 cookies + 2 localStorage origins restored, sessionStorage empty, still landed on the email form).
So step 1 leaves its browser running and step 3 reconnects to that same page over the DevTools protocol.

  Step 1  request   start Chromium as its own process with a DevTools port, connect, open the portal, sign in
                    with the email, wait for the OTP page, then only disconnect: the browser stays open on it.
  Step 2  get_otp   the OTP env var, else poll OTP_COMMAND (e.g. the DB query) until it prints the code.
  Step 3  verify    reconnect to the same browser, find the OTP page, enter the OTP, capture the
                    POST /api/esim/v2/verify-email response, then close that browser.
  Step 4            fail if verify-email returned no data.access_token.

Run as two HyperExecute tests in one dependent group (files.json: request, then verify, on one VM), or by
hand: `python <this file> request`, then `python <this file> verify` on the same machine.
Environment: LOGIN_EMAIL; OTP or OTP_COMMAND; optional OTP_WAIT_SECONDS (default 300), WEB_URL,
HEADLESS (default "true"), CDP_PORT (default 9222), VIDEO_PAUSE_SECONDS (default 5; 0 for none).
"""
import json
import os
import re
import signal
import subprocess
import sys
import time
import urllib.request

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

BASE = os.environ.get("WEB_URL", "https://www.montyesim.com").rstrip("/")
EMAIL = os.environ["LOGIN_EMAIL"]
OUT = os.path.abspath("out")
HEADLESS = os.environ.get("HEADLESS", "true").lower() != "false"
CDP_PORT = int(os.environ.get("CDP_PORT", "9222"))
VIDEO_PAUSE_MS = int(float(os.environ.get("VIDEO_PAUSE_SECONDS", "5")) * 1000)  # pauses in step 3 for the video
CDP_URL = f"http://127.0.0.1:{CDP_PORT}"
BROWSER_FILE = os.path.join(OUT, "browser.json")  # step 1 -> step 3: the browser's pid and DevTools address


def cdp_ready():
    try:
        with urllib.request.urlopen(CDP_URL + "/json/version", timeout=2) as response:
            return response.status == 200
    except OSError:
        return False


def start_browser(playwright):
    """Chromium as a process of its own (not a child Playwright closes), so it outlives this step."""
    args = [playwright.chromium.executable_path, f"--remote-debugging-port={CDP_PORT}",
            f"--user-data-dir={os.path.join(OUT, 'chromium-profile')}", "--no-first-run",
            "--no-default-browser-check", "--window-size=1280,800", "about:blank"]
    if HEADLESS:
        args.insert(1, "--headless=new")
    if os.name == "nt":
        detached = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        try:  # also leave the runner's Windows job, so ending this test doesn't end the browser
            process = subprocess.Popen(args, creationflags=detached | 0x01000000)  # CREATE_BREAKAWAY_FROM_JOB
        except OSError:
            process = subprocess.Popen(args, creationflags=detached)
    else:
        process = subprocess.Popen(args, start_new_session=True)
    deadline = time.monotonic() + 30
    while not cdp_ready():
        if process.poll() is not None or time.monotonic() > deadline:
            sys.exit(f"step 1: Chromium did not open its DevTools port {CDP_PORT} (exit code {process.poll()})")
        time.sleep(0.5)
    with open(BROWSER_FILE, "w") as f:
        json.dump({"pid": process.pid, "cdp": CDP_URL}, f)
    print(f"step 1: started Chromium pid {process.pid} with DevTools on {CDP_URL}", flush=True)
    return process.pid


def stop_browser(pid):
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
    else:
        try:
            os.killpg(pid, signal.SIGTERM)
        except OSError:
            pass


def accept_cookies(page, timeout=10000):
    """The cookie banner shows a moment after the page loads and covers the form."""
    try:
        page.locator("#rcc-confirm-button").first.wait_for(state="visible", timeout=timeout)
    except PlaywrightTimeoutError:
        return
    page.evaluate("document.querySelector('#rcc-confirm-button')?.click()")  # the chat launcher can sit over it


def request_otp(playwright):
    """Step 1: request the OTP in a browser that stays open on the OTP page for step 3."""
    pid = start_browser(playwright)
    try:
        browser = playwright.chromium.connect_over_cdp(CDP_URL)
        context = browser.contexts[0]
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(BASE + "/")
        accept_cookies(page)
        page.locator("a[href='/sign-in']:visible").first.click()
        page.locator("#signinEmail:visible").first.fill(EMAIL)
        page.locator("input[type='checkbox']:visible").first.click()  # Terms & Conditions
        page.locator("xpath=//button[normalize-space()='Sign in']").locator("visible=true").first.click()
        try:
            page.locator("#pin0:visible").first.wait_for(timeout=45000)
        except PlaywrightTimeoutError:
            page.screenshot(path=os.path.join(OUT, "request_failed.png"))
            print(f"step 1: the OTP page did not appear after Sign in (still on {page.url}; see "
                  f"out/request_failed.png). Is LOGIN_EMAIL a real address? It is: {EMAIL!r}", flush=True)
            raise
        page.evaluate("document.querySelector(\"[role='dialog'] button.times-button\")?.click()")  # 'Email Sent' popup
        page.screenshot(path=os.path.join(OUT, "otp_page.png"))
        print(f"step 1: OTP page open ({page.url}); leaving the browser running for step 3", flush=True)
    except BaseException:
        stop_browser(pid)  # this step failed: don't leave the browser behind
        raise
    # no browser.close(): leaving sync_playwright only disconnects, the Chromium process keeps the page


def get_otp():
    """Step 2: the OTP env var, else the code OTP_COMMAND prints (polled every 5s until OTP_WAIT_SECONDS)."""
    if os.environ.get("OTP", "").strip():
        return os.environ["OTP"].strip()
    command = os.environ.get("OTP_COMMAND")
    if not command:
        sys.exit("step 2: set OTP, or OTP_COMMAND (a command that prints the OTP)")
    deadline = time.monotonic() + int(os.environ.get("OTP_WAIT_SECONDS", "300"))
    while True:
        result = subprocess.run(command, shell=True, capture_output=True, text=True)
        code = result.stdout.strip()
        if result.returncode == 0 and re.fullmatch(r"\d{4,8}", code):
            print("step 2: OTP received")
            return code
        if time.monotonic() >= deadline:
            sys.exit(f"step 2: no OTP from OTP_COMMAND in time (last exit {result.returncode}: "
                     f"{(result.stderr or result.stdout).strip()[:300]})")
        time.sleep(5)


def verify_otp(playwright):
    """Step 3: reconnect to step 1's browser, enter the OTP on its open OTP page; step 4: check the access token."""
    with open(BROWSER_FILE) as f:
        pid = json.load(f)["pid"]
    try:
        if not cdp_ready():
            sys.exit(f"step 3: session continuity FAILED: step 1's browser (pid {pid}) is gone: nothing answers "
                     f"on {CDP_URL}, so the runner ended it when step 1's test finished")
        otp = get_otp()
        browser = playwright.chromium.connect_over_cdp(CDP_URL)
        pages = [p for context in browser.contexts for p in context.pages]
        page = next((p for p in pages if p.url.startswith(BASE)), pages[0] if pages else None)
        if page is None:
            sys.exit("step 3: session continuity FAILED: step 1's browser has no open page")
        try:
            page.locator("#pin0:visible").first.wait_for(timeout=10000)
        except PlaywrightTimeoutError:
            print(f"step 3: session continuity FAILED: reconnected to step 1's browser, but its page is on "
                  f"{page.url} without the OTP fields (see out/resumed_page.png)", flush=True)
            raise
        finally:
            page.screenshot(path=os.path.join(OUT, "resumed_page.png"))
        print(f"step 3: session continuity OK: reconnected to step 1's browser, OTP page still open ({page.url})",
              flush=True)
        page.wait_for_timeout(VIDEO_PAUSE_MS)  # for the recording: the OTP page as step 3 found it
        page.locator("#pin0:visible").first.click()
        page.keyboard.type(otp)  # each digit moves to the next box
        page.wait_for_timeout(VIDEO_PAUSE_MS)  # for the recording: the OTP typed in
        with page.expect_response(lambda r: "/api/esim/v2/verify-email" in r.url
                                  and r.request.method == "POST", timeout=30000) as verify:
            page.locator("xpath=//button[normalize-space()='Verify']").locator("visible=true").first.click()
        response = verify.value
        page.wait_for_timeout(VIDEO_PAUSE_MS)  # for the recording: the site's answer to Verify
        try:
            body = response.json()
        except ValueError:
            body = None
        token = ((body or {}).get("data") or {}).get("access_token")
        # a dummy OTP is rejected here: fail now with the API's answer instead of timing out on the signed-in checks
        assert token, (f"step 4: no access token: verify-email answered {response.status} "
                       f"{json.dumps(body)[:300] if body is not None else response.text()[:300]}")
        page.locator("#pin0").first.wait_for(state="hidden", timeout=20000)
        page.goto(BASE + "/")
        page.locator("xpath=//button[normalize-space()='USD']").locator("visible=true").first.wait_for(timeout=20000)
        page.locator("a[href='/sign-in']:visible").first.wait_for(state="hidden", timeout=15000)  # gone once signed in
        page.screenshot(path=os.path.join(OUT, "signed_in.png"))
        print("step 4: access token captured in step 1's browser session — PASS")
    finally:
        stop_browser(pid)


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode not in ("request", "verify"):
        sys.exit("usage: python hyperexecute_playwright_two_steps.py request|verify")
    os.makedirs(OUT, exist_ok=True)
    with sync_playwright() as playwright:
        (request_otp if mode == "request" else verify_otp)(playwright)


if __name__ == "__main__":
    main()
