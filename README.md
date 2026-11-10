# Web OTP login on HyperExecute, two steps on one VM

This folder runs the montyesim.com OTP login as two separate steps on the same HyperExecute VM:

1. `request`: opens the portal, signs in with the email, and stops on the OTP page.
2. `verify`: gets the OTP, enters it on that same OTP page, and checks that an access token comes back.

## Files

| File | What it is |
|---|---|
| `hyperexecute.yaml` | The HyperExecute job. Runs `request`, then `verify`, on one Windows VM. |
| `files.json` | The order of the two steps: `[["request", "verify"]]`. The YAML reads it. |
| `hyperexecute_playwright_two_steps.py` | The two-step script the job runs. |
| `lambdatest_playwright_storage_state_two_steps.py` | The same flow as one script, in one process. Use it on your own machine. |

## Why both steps must use the same browser

The portal keeps the OTP screen only in the open browser tab. It has no URL of its own and is not saved in
cookies, localStorage or sessionStorage. A new browser, even with `storageState` loaded, opens on the email
form again. So the browser that requests the OTP must also be the one that enters it.

`request` starts Chromium on the VM and leaves it open on the OTP page. `verify` runs next on the same VM,
reconnects to that same browser, and enters the OTP there. The browser is closed at the end of `verify`.

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

The first line only checks the YAML. The second starts the job. The CLI shows one test: `request (+1 Dependent Tests)`.

## What the log shows

| Log line | Meaning |
|---|---|
| `step 1: OTP page open (...); leaving the browser running for step 3` | `request` worked. |
| `step 3: session continuity OK: reconnected to step 1's browser, OTP page still open` | `verify` is back on the same OTP page. |
| `step 4: no access token: verify-email answered 200 {... "Wrong pin" ...}` | Expected with the dummy `000000`. Everything else worked. |
| `step 4: access token captured in step 1's browser session — PASS` | The full login worked, with a real OTP. |
| `step 3: session continuity FAILED: step 1's browser (pid ...) is gone` | The browser did not survive between the two steps. Contact LambdaTest support with the job ID. |
| `step 1: the OTP page did not appear after Sign in` | Check `LOGIN_EMAIL`, and `out/request_failed.png` in the job artifacts. |

Screenshots (`otp_page.png`, `resumed_page.png`, `signed_in.png`) are in the job's `out` artifact.

## Run on your own machine

The one-process script needs no HyperExecute:

```
pip install playwright
python -m playwright install chromium
LOGIN_EMAIL=<email> HEADLESS=false python lambdatest_playwright_storage_state_two_steps.py
```

It asks for the OTP in the terminal, unless `OTP` or `OTP_COMMAND` is set. To run the two-step script by hand on
one machine: `python hyperexecute_playwright_two_steps.py request`, then `python hyperexecute_playwright_two_steps.py verify`.
