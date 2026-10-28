# Portal smoke and keyboard review

Run from the repository root, with Python 3.11 or newer. Everything uses the
standard library plus the pytest test extra (`python -m pip install -e '.[test]'`). These commands need no GPU, SSH server, credentials, downloads,
or network beyond 127.0.0.1.

```powershell
python -m pytest tests/test_web_security.py -q
python tests/ui/smoke.py
python tests/ui/smoke.py --keyboard
python tests/ui/smoke.py --keyboard-only
python tests/ui/smoke.py --contrast-only
python -m pytest -q
wsl -e bash -lc 'cd "<repository>" && python3 -m pytest -q'
```

`smoke.py` finds Google Chrome or Chromium in the usual Windows installation
directories and Linux executable paths. Override discovery with `--chrome PATH`
or the `CHROME` environment variable. An absent browser produces an explicit
skip; a present browser that fails to start or reports a JavaScript error fails
the run. The contrast check always runs, even when Chrome is absent. Browser
smoke is a separate command so the ordinary test run needs no browser.

The script prints a retained temporary screenshot directory. `--output PATH`
selects another directory. There are 36 PNGs: nine hash routes, two themes, and
1440×1000 / 390×1000 viewports. It uses `--headless=new` and DevTools
`Emulation.setDeviceMetricsOverride`, then asserts the actual
`document.documentElement.clientWidth`. The narrow screenshots are real
viewports, with no cropping or forced mockup class. It also rejects document
overflow and waits for API rendering before capturing. Compare these images
beside `docs/ui/img/`; the sample figures there are not expected measurements.

DevTools discovery uses loopback HTTP (`--remote-debugging-port`); commands and
events use a small RFC 6455 transport written with `socket`, `struct`, and
`http.client`. There is no third-party websocket module. Runtime monitoring is
enabled before navigation. `Runtime.exceptionThrown`, console `error`, and failed
console assertions fail the run. Screenshots include both system colour-scheme
preferences. The browser uses a fresh temporary profile, disables GPU and
background services, bypasses proxies, and rejects non-loopback DNS names.

The fixture process invokes the actual `cli.main(["ui", ...])`, with real HTTP
request validation, job scheduling, audit writes, JSON rendering, and static
files. It replaces config loading, orchestration mutations/status/logs, remote
doctor/GPU calls, secret rotation, and engine telemetry at their source
boundaries. Adapter JSON comes from `tests/fixtures/adapters/`, with request
timestamps rebased to fixture startup so the ten-minute quantile window has
samples. Node replies use the `hyperloom/node-v1` contract and a synthetic
accelerator. A temporary all-zero public-key blob supplies the pinned-machine
state without a private key or SSH peer. These are synthetic/replayed test sources, not
observations of machines. The real portal API still creates all seven documents
according to UI-SPEC §9, including `null` and `why` for absent sources. Subprocess
execution and non-loopback socket connections/binds are refused inside the
fixture process. All config, audit, runtime and call-recording files live in a
temporary directory that is removed when the fixture stops.

The unittest security checks launch a fresh fixture CLI process for each test.
They cover every read endpoint, foreign/missing/malformed Origin, rebinding
Host, both allowed Host spellings, missing/wrong/cross-launch CSRF tokens,
GET/other methods on mutations, JSON content type, malformed/duplicate/nonfinite
JSON, the 64 KiB body limit, no CORS/preflight grants, static path traversal,
duplicate security headers, invalid body framing, concurrent-job refusal,
immediate stop/cancel, and polled/audited completion.
Rejected requests must leave fake mutation call records empty. The contrast test
parses the actual `app.css` tokens and checks 30 text/background combinations at
4.5:1: primary/secondary/label text on the three ordinary surfaces, selected
accent text, primary-button text, and four status pill pairs, in both themes.
It does not substitute for inspecting focus indicators, charts or all composited
styles in the rendered page.

## Runnable keyboard walkthrough

`python tests/ui/smoke.py --keyboard` adds an automated desktop keyboard pass to
the 28 screenshots. It uses DevTools key events to Tab to each of the five
mutation dialog openers, open with Space, check the dialog name and focus trap
in both directions, wait through a poll, close with Escape, and check focus return. Rotation also
gets a valid synthetic key name and an Enter attempt that must not submit.
Confirmations and Stop/Cancel are exercised by the manual walkthrough below;
the HTTP suite separately tests their backend dispatch and priority.
Use `--keyboard-only` to repeat the dialog checks without taking screenshots.

Start the same isolated fixture server and open the printed loopback URL in a
browser. Keep the terminal running; Ctrl+C stops the server and removes its
temporary files. No action in this fixture can operate a real machine.

```powershell
python tests/ui/smoke.py --serve
```

Use only Tab, Shift+Tab, arrows, Space, Enter and Escape after opening the URL.
Repeat at desktop width and at a real 390 px viewport, in Light and Dark. Record
browser/version, theme/viewport, pass/fail, and any focused element that loses its
ring or becomes unreachable.

1. Tab to **Skip to content**, activate it, then walk all seven navigation links.
   Each route must have its matching heading and current-page indication. At
   narrow width, reach the named Menu button; Space opens navigation, Escape
   closes it, and focus remains usable. Reach Appearance and switch both themes
   with the native select control.
2. On Devices, activate **Add by address**. The dialog must announce its name,
   show labelled machine/fingerprint fields and list the effects of pinning.
   Tab and Shift+Tab repeatedly: focus must stay in the open dialog. Escape must
   close it and return focus to its opener. Reopen, enter `halo-1` and a synthetic
   fingerprint (for example `SHA256:fixture`), then activate the confirm button
   with Space. The fake scan deliberately returns no keys: a clear failure must
   appear, remain keyboard reachable, and be announced. No real key is pinned.
3. On Engines, open **Start**. Reach **Router runs on**, select this machine and
   then `halo-1`, and reach each node/device checkbox. Verify the profile defaults
   and listed start effects. Escape and reopen to check focus return. Activate
   confirmation with Space; the fixture completes without starting a process.
   Tab to **Stop** and activate it. Stop must proceed immediately, without a
   confirmation delay. Repeat Start from Models if offered there.
4. On Engines, open **Install**. Read the profile/build-machine labels and effects,
   cancel with Escape, reopen, then explicitly confirm with Space. The fake
   install must finish and its progress/result must be reachable. Open Doctor,
   select `halo-1`, confirm, and verify the result. A local doctor is deliberately
   refused by the fixture's subprocess guard; selecting `halo-1` uses fake remote
   inspection and needs no SSH.
5. On Settings, open **Rotate keys**. Read the key-name label and restart effect.
   Press Enter while focus is in the destructive dialog: it must not submit.
   Escape must cancel and restore focus. Reopen, choose a synthetic valid key
   name such as `fixture-engine`, and explicitly activate confirmation with
   Space. Rotation must finish without showing key material.
6. To review a running job and **Cancel**, start the blocked fixture below in a
   second terminal, open its printed URL, and start Qwen. Attempt another mutation:
   it must stay disabled with a reason while Stop and Cancel stay reachable.
   Activate Cancel with Space and verify prompt completion. Restart this blocked
   fixture before repeating with Stop. Its gate is released by either action.

   ```powershell
   python tests/ui/smoke.py --serve --blocked
   ```

7. Walk every enabled mutation offered by the build. Pair, discovery and agent
   revocation must say **later phase** and name their blockers. For any disabled
   parameter save, rollback, disconnect, model hash/import or link-probe control,
   verify that the explanation is visible and no action occurs. If one becomes
   enabled, review its effects, Escape/focus return, field errors, explicit
   confirmation and resulting audit/progress before marking this step passed.
   Rollback must never offer reservations, approvals, pairings or secrets.
8. On Logs, Tab into the log region and scroll with arrows/Page Down. Filters
   must have names; narrow logs must wrap and scroll inside their card. Reach
   failure actions without using a pointer. On Stats, reach chart table toggles
   and confirm units, sample counts, and source labels remain understandable.
9. Background the tab for at least three poll intervals, then return. Watch
   mutations remain disabled until authoritative state arrives. If testing an
   offline/reconnect state, stop/restart the fixture server on the same port
   (`--port N`); stale data must be labelled and controls must wait for refresh.

For the required NVDA smoke pass on Windows, enable NVDA and repeat steps 1–8.
Check dialog names, control labels, associated field errors, focus return, and
new-warning/error announcements. Numeric polling must not repeatedly announce
values. This is a manual reviewer check; the automated script does not claim an
NVDA or full accessibility pass. Record findings before Phase 1b acceptance.

Measured results reuses the running-profile, stats and observability replay fixtures. Its captures check published source badges and separately labelled live values alongside the packaged README measurements.

The sanitized inspection fixtures in `tests/fixtures/web/inspection.json` cover
workstation plus halo-1/halo-2, unified GPU-visible memory, the flat versioned
USB4 layout, and one cable warning. Smoke assertions reject raw OS/stream
dumps, unresolved ports, duplicate nav icons, incorrect degradation, and missing
launch argv/env. ROCm packages start collapsed; the topology map is replaced
by the link list on narrow screens.

## Recipes & profiles walkthrough

The default run ends with an automated walkthrough of `#/recipes` (desktop): open the built-in Qwen recipe, type an
out-of-range value (the field shows its inline error and is described by it), make the recipe invalid (the reader's
refusal is shown), reset and change a value (no error; the explain answer and its CLI line appear), preview the
placement (the answer and its `hyperloom recipe plan` line appear; Deploy is then either enabled or disabled with the
preview's refusal as its reason, and an enabled Deploy is confirmed and its job shown), and save a copy into the user
directory (it appears in the list). In the fixture, recipe requests run the CLI's parser and `commands.run` in the
fixture process (subprocesses are refused there) on the stub reader; the API tests (`tests/test_web_recipes.py`)
run the real child processes.
