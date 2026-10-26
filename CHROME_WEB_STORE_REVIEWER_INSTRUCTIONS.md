# Chrome Web Store reviewer instructions — Neo Saarthi

Extension ID: `polchmmnbnamnpkoiipldgdngmdllgbc`
Support: info@neosaarthi.si
Source and setup: https://github.com/Neo-Saarthi/neo-saarthi-browser

## Review requirements

Use Windows with a current Chrome installation, Python 3.12 or 3.13, and the extension under review. This repository supplies the local browser app; Electron and the private application source are not required. There is no application account or login.

Full task execution requires working OpenCode model access. The developer must provide a limited, funded reviewer API key privately in the Chrome Web Store Test instructions field before submission. No API key or portal credentials are included in this repository. Without model access, pairing and connection controls can be checked, but the task demonstration cannot be completed.

## Start the browser app

Download this repository using **Code → Download ZIP**, extract it and open PowerShell in the extracted folder. Run:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe run.py
```

Open **http://127.0.0.1:8000**. In **Settings**, enter the separately supplied reviewer API key and save it. Leave CAMS and Karvy credentials empty. Keep this process running during the review. It binds only to this computer: app port 8000 and extension bridge port 41691. Close any other Neo Saarthi backend using those ports first.

The Playwright browser download is only needed for the repository's automated tests; it is not needed for this manual extension demonstration.

## Pair the extension

1. Use the Chrome Web Store extension under review. Do not install a second unpacked extension from another project.
2. Open its toolbar popup, read the data disclosure and choose whether to accept it.
3. In the browser app, click **Generate pairing code**.
4. Enter that code in the extension and click **Connect browser**. Codes expire after ten minutes and work once.
5. Confirm that the popup reports **Connected to Neo Saarthi** and the app shows a connected browser.
6. In the extension, choose **Allow HTTPS websites** and review Chrome's permission prompt. Keep saved-password entry disabled.

If a code expires or is rejected, generate a new one. Do not use codes from screenshots or previous sessions.

## Safe task demonstration

In the app, keep **Saved credentials** set to **None — everyday task**, then run:

> Open https://example.org in my connected browser, read the page heading, and tell me what it says. Do not sign in, submit forms or download anything.

Expected behavior: a managed browser tab opens on the public example site, and the task returns the page heading. Check the actual page against the result. Keep Chrome visible; native input can stop if it cannot verify the foreground browser.

## Pause and resume

Run this harmless multi-step task:

> Open https://example.org and read its heading. Then open https://example.com and read its heading. Tell me both headings. Do not sign in, submit forms or download anything.

While it is running, click **Pause** in the app. Further agent input should stop; a completed action is not undone. Click **Resume** within five minutes while the app and browser stay running. The same task should continue using its retained goal and history. A short task may finish before Pause is clicked; that does not exercise pause/resume.

If pausing through **Pause Browser Use** in the extension popup, resume through that same popup. App pause and extension connection pause are separate controls. Restarting the app loses in-memory task continuation.

## Access and connection controls

- **Take over current tab** in the extension releases that tab from automation.
- **Disconnect this browser** in the app revokes the connection. A new pairing code is required to connect again.
- Chrome's extension settings can revoke website access. New tasks needing that access should stop until it is granted again.
- Closing a managed tab should remove it from the popup's shared-tab count.

## Data and saved-password feature

Browser task context can be sent by the local app to JEV and DeepSeek through the configured provider. It is not all confined to this computer. Review the published privacy policy at **https://neosaarthi.si/privacy-policy/**; the developer must verify that this URL is publicly accessible before submission.

The basic demonstration does not require financial websites, passwords, CAPTCHA solving or report submission. The saved-password feature is disabled for this demonstration. Feature-specific review needs a separately approved safe test arrangement; do not use real CAMS/Karvy accounts or send financial reports to test it.

## Troubleshooting

| Problem | What to check |
| --- | --- |
| Waiting for connection | App is running; ports 8000 and 41691 are available. |
| Pairing fails | Generate a fresh code; confirm the extension ID above. |
| Website access blocked | Review the extension's HTTPS website access and site restrictions. |
| Mouse input stops | Chrome is visible, the intended tab is in front, and the task is not paused. |
| Task cannot call a model | Confirm the privately supplied reviewer key is valid and has the required model access. Contact support if missing. |
| Task does not resume | Resume within five minutes without restarting the backend; resume in the popup if paused there. |

## Developer checklist before submission

- Supply funded, limited reviewer model access privately. Never paste that key into this file or GitHub.
- Verify the public privacy-policy URL and Store privacy declarations.
- Check these instructions against the final extension package and app revision.
- Complete any remaining production pairing lifecycle checks. Existing test records are not a guarantee of universal reliability.
