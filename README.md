# Neo Saarthi Browser

Run browser tasks with the Neo Saarthi extension. Connect your browser, describe a task and follow its progress. JEV handles browser decisions; DeepSeek handles text and vision. Pause and resume keep the current task context while the app stays running.

## Requirements

- Python 3.12 or 3.13 and Windows for native mouse and password entry.
- Chrome or Edge with the Neo Saarthi extension installed. Store ID: `polchmmnbnamnpkoiipldgdngmdllgbc`.
- Your own OpenCode API key with access to the configured models. Provider charges and terms apply.
- Ports 8000 and 41691 available. Run only one Neo Saarthi browser backend at a time.

## Install and run

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe run.py
```

Open **http://127.0.0.1:8000**. Save your API key in Settings, or use a local `.env` based on `.env.example`. Stop the app with Ctrl+C in its terminal. No Node.js, Bun or Electron installation is required.

## Connect your browser

1. Open the Neo Saarthi extension and review its data disclosure.
2. Click **Generate pairing code** in the app.
3. Enter the code in the extension and connect. Codes expire after ten minutes and work once.
4. Approve the website access you need in the extension.
5. Enter a task and click **Run task**. Keep the browser visible and avoid other mouse or keyboard activity during native input.

The extension is installed separately. This repository does not bundle a second extension. The app listens only on this computer and requires no application account. A local session token protects app requests; extension connections require pairing.

Use **Pause** and **Resume** in the app. If you pause in the extension popup, resume there. Task context is retained for up to five minutes while the process stays running; restarting the app loses in-memory task continuation.

## Saved credentials and data

Settings supports CAMS email, Karvy username/password and report extraction passwords. Choose a credential profile only for the relevant task. Blank fields keep saved values; **Clear saved values** removes them. Website login and permission requirements still apply. Review task wording carefully: tasks can request report submission.

Settings are encrypted locally, with a Windows DPAPI-protected encryption key on Windows. The settings API returns configuration status rather than saved secrets. This does not protect against malware running as your user.

Connections, preferences, audit metadata and workflows are stored in `data/`. Logs and learned trajectories can contain browser content. Disconnecting or clearing Settings does not erase all logs or learned data.

Task context and images can be sent to JEV and DeepSeek through the configured provider. Browser processing is not necessarily local-only. Do not grant access to pages you do not want processed.

## Project layout

```text
run.py               App and local bridge startup
companion/           App API, encrypted settings and runtime
backend/             Browser engine, JEV, recipes, bridge and storage
ui/                  Browser task interface
tools/               Repository credential check
requirements.txt     Python dependencies
.env.example         Settings template without credentials
data/                Private local settings and runtime data; ignored
```

## Repository hygiene

Never commit `.env`, `data/`, `.venv/`, browser profiles, logs or test artifacts. Share `.env.example` without secrets. Before committing changes, run:

```powershell
.\.venv\Scripts\python.exe tools\check_repository.py
```

The owner has not yet selected a distribution license.
