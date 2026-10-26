# Verification results

26 October 2026. All five acceptance gates met; none abandoned.

- 79 automated local tests passed in 8.848 seconds. These include isolated synthetic calendar/recipient/password tests; no real CAMS/Karvy workflow was run.
- Live ordinary browser task: paired through the real extension toolbar popup, entered Buy notebooks, clicked Save note and independently verified the resulting DOM. Completed in 16.0 seconds.
- Live pause/resume task: entered the same task once, paused it, verified no DOM changes for 2.5 seconds, resumed and verified Note saved: Pack the charger. Same task ID and retained continuation history. Completed in 17.3 seconds including the pause.
- Native Windows input enabled. Actual JEV and DeepSeek requests returned HTTP 200. Providers were observed, not mocked. No Muse route was used.
- Unchanged 19-file extension uses the original fixed :41691 protocol and endpoints. Companion UI/API runs on :8000.
- 801 original source files matched their initial hashes. 58 copied files remain byte-for-byte identical, including 39 engine/support files and 19 extension files. Three copied files received documented integration/environment adaptations.
- A relocated copy imported under Python isolated mode using only standalone source paths.
- Desktop/mobile UI screenshots inspected; the mobile page has no horizontal overflow. Pairing, form submission, resume and policy-controlled loopback website access were exercised through actual UI/API and extension behavior.

The test loop fixed a CSP-incompatible test wait, replaced invalid popup-as-tab testing with the actual toolbar popup, and fixed the new companion pause adapter to use the existing native pause path. The extension and original engine's task loop were not changed. No single unresolved failure was retried five times.

Limitations: no real financial-site test; no clean second computer/OS certification; no live vision fallback was required by the simple note tasks (calendar/vision payload paths are covered locally). The full regression/provider evidence and screenshots are local artifacts in ignored test-results/, not public account access. The app still requires Python, installed dependencies, browser permission approval and your own API access. No application login, source publication, Chrome Store submission or installer distribution was performed.

## Local environment and repository preparation

The companion now has its own ignored `.env` with the project's OpenCode API key; financial-portal credentials were not copied. A fresh live run loaded that local file directly, paired through the toolbar popup and completed both ordinary tasks, including pause/resume. Latest measured durations: 17.7 seconds and 16.3 seconds. Actual JEV and DeepSeek calls succeeded. Git ignore/credential checks are recorded in the ignored repository-check.json artifact. No files are staged, no commits exist and no remote is configured.

## Cleanup and external extension packaging — 26 October 2026

The extension is now distributed separately. Its redundant 19-file source folder was removed from this repository. Test tools accept an external ZIP or unpacked directory; production runtime does not need either path. Engine provenance now covers 39 exact copies and three documented adapters. The existing pairing protocol, fixed ports, hybrid engine and pause/resume implementation were not changed by cleanup.

Temporary root test logs, generated caches and private portal-test helpers/logs were removed. The local model key, encrypted saved credentials, runtime data, Python environment and Git metadata were retained. Nothing was committed or pushed.

The supplied uploaded 0.7.1 ZIP differs from the previous local extension in manifest key and disclosure/configuration metadata; executable scripts match. It remains unchanged. Unpacked test loading adds the public Store key only to a disposable extracted manifest.

Pairing-only checks exercised actual toolbar popup pairing, invalid-code rejection and used-code rejection. A first run also reached backend and browser restart reconnection, then failed while reopening the popup for revocation. A follow-up run stopped with a closed browser target after backend restart. The repeated run was stopped at the user's request; revocation/re-pairing coverage is incomplete. These failures do not establish a backend pairing defect, but this check is not a complete pass. No further browser or hybrid tests were run after the stop request.

The earlier statement that no portal credentials had been copied describes the initial setup only. At the owner's later request, portal credentials were saved in the local encrypted settings. A guarded CAMS run was observed progressing, but complete CAMS/Karvy compatibility and report submission were not certified.
