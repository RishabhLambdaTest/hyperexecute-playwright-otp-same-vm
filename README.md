# Web OTP login on HyperExecute, two steps on one VM

This folder runs the montyesim.com OTP login as two separate steps on the same HyperExecute VM, in Chromium,
Firefox and WebKit (the engine behind Safari):

1. `request`: opens the portal, signs in with the email, and stops on the OTP page.
2. `verify`: gets the OTP, enters it on that same OTP page, and checks that an access token comes back.

## Files

| File | What it is |
|---|---|
| `hyperexecute.yaml` | The HyperExecute job. For each browser, runs `request`, then `verify`, on one Windows VM. |
| `files.json` | The step pairs, one per browser, e.g. `["request-firefox", "verify-firefox"]`. Each pair runs in order on its own VM. Remove a pair to skip that browser. |
| `hyperexecute_playwright_two_steps.py` | The two-step script the job runs. |
| `lambdatest_playwright_storage_state_two_steps.py` | The same flow as one script, in one process. Use it on your own machine. |

## Why both steps must use the same browser

The portal keeps the OTP screen only in the open browser tab. It has no URL of its own and is not saved in
cookies, localStorage or sessionStorage. A new browser, even with `storageState` loaded, opens on the email
form again. So the browser that requests the OTP must also be the one that enters it.

`request` starts a small background process on the VM that opens the browser, signs in and waits on the OTP
page; `request` itself then ends. `verify` runs next on the same VM, sends the OTP to that background process,
and it types the OTP on the same page. The browser is closed at the end of `verify`. This does not depend on the
browser type, so it works the same in Chromium, Firefox and WebKit.

Real Safari: Playwright drives WebKit, not the Safari app. Real Safari needs Selenium on a macOS machine.

## Before you run

- The `hyperexecute` CLI on your machine: https://www.lambdatest.com/support/docs/hyperexecute-cli-run-tests-on-hyperexecute-grid/
- Your LambdaTest username and access key.
- The dependent scenarios feature enabled for your organization (ask LambdaTest support).

## Settings (under `env` in `hyperexecute.yaml`)

| Setting | What to put |
|---|---|
| `LOGIN_EMAIL` | The test account's email. |
| `OTP` | A fixed code. The YAML ships with the dummy `000000`. |
| `OTP_COMMAND` | For real runs, instead of `OTP`: a command that prints only the code, for example a `curl` to an endpoint that reads it from your database. `verify` runs it every 5 seconds until it prints a 4 to 8 digit code. |
| `OTP_WAIT_SECONDS` | How long `verify` keeps trying `OTP_COMMAND`. Default 300. |
| `HEADLESS` | `"false"` shows the browser on the VM, so it appears in the job video. Default `"true"`. |
| `BROWSER` | Only for the one-process script: `chromium` (default), `firefox` or `webkit`. The two-step script takes the browser from the test name in `files.json`. |
| `VIDEO_PAUSE_SECONDS` | Pause at the OTP page, after typing the code and after Verify, for the video. Default 5. `"0"` for none. |

Note: the HyperExecute VM must be able to reach whatever `OTP_COMMAND` calls. A database that is only on your
VPN is not reachable from the VM; expose a small HTTP endpoint for the OTP (a LambdaTest Tunnel can route the
VM's HTTP calls to an internal one), or use a fixed OTP for the test account.

## Run

From this folder:

```
hyperexecute --user <username> --key <access key> --validate --config hyperexecute.yaml
hyperexecute --user <username> --key <access key> --config hyperexecute.yaml
```

The first line only checks the YAML. The second starts the job. The CLI shows one test per browser, e.g. `request-firefox (+1 Dependent Tests)`.

## What the log shows

| Log line | Meaning |
|---|---|
| `step 1: firefox OTP page open (...); the browser stays open for step 3` | `request` worked. |
| `step 3: session continuity OK: step 1's firefox page was still on the OTP screen` | `verify` is back on the same OTP page. |
| `step 4: no access token: verify-email answered 200 {... "Wrong pin" ...}` | Expected with the dummy `000000`. Everything else worked. |
| `step 4: access token captured in step 1's firefox session — PASS` | The full login worked, with a real OTP. |
| `step 3: session continuity FAILED: step 1's firefox keeper is gone` | The browser did not survive between the two steps. Contact LambdaTest support with the job ID. |
| `step 1: the OTP page did not appear in firefox` | Check `LOGIN_EMAIL`, `out/firefox_request_failed.png` and `out/keeper-firefox.log` in the job artifacts. |

Screenshots (`<browser>_otp_page.png`, `<browser>_resumed_page.png`, `<browser>_signed_in.png`) and the keeper log
(`keeper-<browser>.log`) are in the job's `out` artifact.

## Run on your own machine

The one-process script needs no HyperExecute:

```
pip install playwright
python -m playwright install chromium firefox webkit
BROWSER=firefox LOGIN_EMAIL=<email> HEADLESS=false python lambdatest_playwright_storage_state_two_steps.py
```

It asks for the OTP in the terminal, unless `OTP` or `OTP_COMMAND` is set. To run the two-step script by hand on
one machine: `python hyperexecute_playwright_two_steps.py request-firefox`, then `python hyperexecute_playwright_two_steps.py verify-firefox`.
