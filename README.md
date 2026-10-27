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

Install the Neo Saarthi extension and accept its first-use data disclosure. Chrome asks for HTTPS website access during installation. With this app running, the extension connects automatically; there is no pairing code or separate HTTPS button.

The app registers a Windows native messaging helper for the official Store ID in your user account. Only that extension can use the helper. The helper authenticates to this app using a per-launch secret encrypted locally; the ordinary HTTP auto-connect endpoint remains disabled. No administrator access is required. If your organization blocks native messaging, setup must be permitted by your administrator.

The extension popup shows connection status. Use **Pause** and **Resume** in the app or the existing keyboard shortcut. Task context is retained for up to five minutes while the process stays running; restarting the app loses in-memory task continuation. Keep the browser visible during native input.

Use **Disconnect browsers** in the app to disable automatic connection and revoke existing browser connections. **Enable automatic connection** permits them again. A connected browser is selected automatically; a selector appears if multiple browser profiles are available.

## Saved credentials and data

Settings supports CAMS email, Karvy username/password and report extraction passwords. Saved-password entry is disabled until you explicitly approve it in Settings and save. Uncheck the approval and save to revoke it. Revoking during a task also pauses native input; resume explicitly if you want to continue other work. The extension checks current approval before each password operation. Choose a credential profile only for the relevant task. Blank fields keep saved values; **Clear saved values** removes them. Website login and permission requirements still apply. Review task wording carefully: tasks can request report submission.

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
