"""
Web OTP login with Playwright split over two steps that share ONE live browser on the same machine (the
HyperExecute VM, or your laptop). Works with chromium, firefox and webkit.

The portal keeps its OTP stage only in the open page's memory, so a new browser session (even with storageState)
lands back on the email form. Step 1 therefore starts a small background process (the keeper) that opens the
browser, signs in and waits on the OTP page; step 3 hands it the OTP over localhost and it types it on that page.

  Step 1  request-<browser>   start the keeper, wait until it reports the OTP page, exit (the keeper stays up).
  Step 2  get_otp             the OTP env var, else poll OTP_COMMAND until it prints the code.
  Step 3  verify-<browser>    send the OTP to the keeper, which enters it on step 1's page and closes the browser.
  Step 4                      fail if verify-email returned no data.access_token.

Environment: LOGIN_EMAIL; OTP or OTP_COMMAND; optional OTP_WAIT_SECONDS (default 300), WEB_URL,
HEADLESS (default "true"), VIDEO_PAUSE_SECONDS (default 5), KEEPER_PORT (default 9333),
KEEPER_IDLE_SECONDS (default 900).
"""
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

BASE = os.environ.get("WEB_URL", "https://www.montyesim.com").rstrip("/")
OUT = os.path.abspath("out")
HEADLESS = os.environ.get("HEADLESS", "true").lower() != "false"
VIDEO_PAUSE_MS = int(float(os.environ.get("VIDEO_PAUSE_SECONDS", "5")) * 1000)
KEEPER_IDLE_SECONDS = int(os.environ.get("KEEPER_IDLE_SECONDS", "900"))
BROWSERS = ("chromium", "firefox", "webkit")


def keeper_port(browser):
    return int(os.environ.get("KEEPER_PORT", "9333")) + BROWSERS.index(browser)


def files(browser):
    return (os.path.join(OUT, f"keeper-{browser}.json"), os.path.join(OUT, f"keeper-{browser}.log"))


def accept_cookies(page, timeout=10000):
    try:
        page.locator("#rcc-confirm-button").first.wait_for(state="visible", timeout=timeout)
    except PlaywrightTimeoutError:
        return
    page.evaluate("document.querySelector('#rcc-confirm-button')?.click()")


def open_otp_page(page, email):
    page.goto(BASE + "/")
    accept_cookies(page)
    page.locator("a[href='/sign-in']:visible").first.click()
    page.locator("#signinEmail:visible").first.fill(email)
    page.locator("input[type='checkbox']:visible").first.click()
    page.locator("xpath=//button[normalize-space()='Sign in']").locator("visible=true").first.click()
    page.locator("#pin0:visible").first.wait_for(timeout=45000)
    page.evaluate("document.querySelector(\"[role='dialog'] button.times-button\")?.click()")


def enter_otp(page, otp, browser):
    page.locator("#pin0:visible").first.wait_for(timeout=10000)
    page.screenshot(path=os.path.join(OUT, f"{browser}_resumed_page.png"))
    page.wait_for_timeout(VIDEO_PAUSE_MS)
    page.locator("#pin0:visible").first.click()
    page.keyboard.type(otp)
    page.wait_for_timeout(VIDEO_PAUSE_MS)
    with page.expect_response(lambda r: "/api/esim/v2/verify-email" in r.url
                              and r.request.method == "POST", timeout=30000) as verify:
        page.locator("xpath=//button[normalize-space()='Verify']").locator("visible=true").first.click()
    response = verify.value
    page.wait_for_timeout(VIDEO_PAUSE_MS)
    try:
        body = response.json()
    except ValueError:
        body = None
    token = ((body or {}).get("data") or {}).get("access_token")
    result = {"status": response.status, "token": bool(token),
              "body": json.dumps(body)[:300] if body is not None else response.text()[:300]}
    if token:
        page.locator("#pin0").first.wait_for(state="hidden", timeout=20000)
        page.goto(BASE + "/")
        page.locator("a[href='/sign-in']:visible").first.wait_for(state="hidden", timeout=15000)
        page.screenshot(path=os.path.join(OUT, f"{browser}_signed_in.png"))
    return result


def keep(browser_name):
    """Background process: owns the browser for both steps and waits for the OTP over localhost HTTP."""
    state_file, _ = files(browser_name)
    port = keeper_port(browser_name)

    def write_state(**state):
        with open(state_file, "w") as f:
            json.dump({"pid": os.getpid(), **state}, f)

    with sync_playwright() as playwright:
        browser = getattr(playwright, browser_name).launch(headless=HEADLESS)
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        api_calls = []

        def record(response):
            if "/api/" in response.url and response.request.method != "GET":
                try:
                    body = response.text()[:300]
                except Exception:
                    body = ""
                api_calls.append(f"{response.request.method} {response.url.split('?')[0]} -> {response.status} {body}")
                print(f"keeper: {api_calls[-1]}", flush=True)

        page.on("response", record)
        try:
            open_otp_page(page, os.environ["LOGIN_EMAIL"])
        except Exception as error:
            page.screenshot(path=os.path.join(OUT, f"{browser_name}_request_failed.png"))
            try:
                alerts = page.locator("[role='alert'], [role='dialog'], .Toastify__toast, .error, .invalid-feedback").all_inner_texts()
            except Exception:
                alerts = []
            write_state(state="failed", url=page.url, error=str(error).splitlines()[0][:200],
                        api_calls=api_calls[-5:], messages=[m.strip()[:200] for m in alerts if m.strip()][:5])
            browser.close()
            return
        page.screenshot(path=os.path.join(OUT, f"{browser_name}_otp_page.png"))
        write_state(state="otp_page", url=page.url)
        print(f"keeper: OTP page open ({page.url}), waiting for the OTP on port {port}", flush=True)

        holder = {}

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                otp = json.loads(self.rfile.read(int(self.headers["Content-Length"])))["otp"]
                if not page.url.startswith(BASE) or page.locator("#pin0:visible").count() == 0:
                    holder["result"] = {"continuity": False, "url": page.url}
                else:
                    try:
                        holder["result"] = {"continuity": True, "url": page.url, **enter_otp(page, otp, browser_name)}
                    except Exception as error:
                        holder["result"] = {"continuity": True, "url": page.url, "error": str(error)[:300]}
                payload = json.dumps(holder["result"]).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        server = HTTPServer(("127.0.0.1", port), Handler)
        server.timeout = 1
        deadline = time.monotonic() + KEEPER_IDLE_SECONDS
        while "result" not in holder and time.monotonic() < deadline:
            server.handle_request()
        server.server_close()
        browser.close()
        print(f"keeper: done {holder.get('result', 'no OTP received in time')}", flush=True)


def request_otp(browser_name):
    """Step 1: start the keeper as its own process, wait until it reports the OTP page, then exit."""
    state_file, log_file = files(browser_name)
    for path in (state_file,):
        if os.path.exists(path):
            os.remove(path)
    args = [sys.executable, "-u", os.path.abspath(__file__), f"keep-{browser_name}"]
    log = open(log_file, "w")
    if os.name == "nt":
        detached = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        try:
            process = subprocess.Popen(args, stdout=log, stderr=log, stdin=subprocess.DEVNULL,
                                       creationflags=detached | 0x01000000)
        except OSError:
            process = subprocess.Popen(args, stdout=log, stderr=log, stdin=subprocess.DEVNULL,
                                       creationflags=detached)
    else:
        process = subprocess.Popen(args, stdout=log, stderr=log, stdin=subprocess.DEVNULL, start_new_session=True)
    print(f"step 1: started the {browser_name} keeper, pid {process.pid}", flush=True)
    deadline = time.monotonic() + 120
    while not os.path.exists(state_file):
        if process.poll() is not None or time.monotonic() > deadline:
            log.close()
            sys.exit(f"step 1: the keeper stopped before the OTP page (exit {process.poll()}):\n"
                     + open(log_file).read()[-2000:])
        time.sleep(0.5)
    time.sleep(0.2)
    with open(state_file) as f:
        state = json.load(f)
    if state["state"] != "otp_page":
        sys.exit(f"step 1: the OTP page did not appear in {browser_name} (still on {state.get('url')}): "
                 f"{state.get('error')}\n  site API calls after Sign in: {state.get('api_calls') or 'none'}"
                 f"\n  messages on the page: {state.get('messages') or 'none'}"
                 f"\n  see out/{browser_name}_request_failed.png")
    print(f"step 1: {browser_name} OTP page open ({state['url']}); the browser stays open for step 3", flush=True)


def get_otp():
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
            print("step 2: OTP received", flush=True)
            return code
        if time.monotonic() >= deadline:
            sys.exit(f"step 2: no OTP from OTP_COMMAND in time (last exit {result.returncode}: "
                     f"{(result.stderr or result.stdout).strip()[:300]})")
        time.sleep(5)


def verify_otp(browser_name):
    """Step 3: hand the OTP to the keeper, which enters it on the page step 1 left open; step 4: check the token."""
    keeper_url = f"http://127.0.0.1:{keeper_port(browser_name)}"
    try:
        urllib.request.urlopen(keeper_url, timeout=2)
    except urllib.error.HTTPError:
        pass
    except OSError:
        sys.exit(f"step 3: session continuity FAILED: step 1's {browser_name} keeper is gone "
                 f"(nothing answers on {keeper_url})")
    otp = get_otp()
    request = urllib.request.Request(keeper_url, data=json.dumps({"otp": otp}).encode(),
                                     headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=180) as response:
        result = json.load(response)
    if not result["continuity"]:
        sys.exit(f"step 3: session continuity FAILED: {browser_name} page is on {result['url']} without the OTP fields")
    print(f"step 3: session continuity OK: step 1's {browser_name} page was still on the OTP screen ({result['url']})",
          flush=True)
    if result.get("error"):
        sys.exit(f"step 3: entering the OTP failed: {result['error']}")
    if not result["token"]:
        sys.exit(f"step 4: no access token: verify-email answered {result['status']} {result['body']}")
    print(f"step 4: access token captured in step 1's {browser_name} session — PASS")


def main():
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    mode, _, browser_name = arg.partition("-")
    browser_name = browser_name or os.environ.get("BROWSER", "chromium")
    if mode not in ("request", "verify", "keep") or browser_name not in BROWSERS:
        sys.exit("usage: python hyperexecute_playwright_two_steps.py request|verify[-chromium|-firefox|-webkit]")
    os.makedirs(OUT, exist_ok=True)
    if mode == "keep":
        keep(browser_name)
    elif mode == "request":
        request_otp(browser_name)
    else:
        verify_otp(browser_name)


if __name__ == "__main__":
    main()
