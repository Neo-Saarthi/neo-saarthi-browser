# Independent browser companion

1. Copy the current JEV/browser-use engine, task service, protocol schemas and unchanged extension into this folder. Record source hashes and necessary adapter changes.
2. Add standalone local storage, encrypted settings, an automatic local identity and loopback startup on ports 8000 and 41691. Keep extension endpoints and messages unchanged. No application login.
3. Build a new local HTML/CSS/JavaScript UI for pairing, connection selection/revocation, arbitrary tasks, encrypted CAMS/Karvy credentials, pause/resume and task results.
4. Run security, protocol, storage and copied-engine regressions in isolation. Run the unchanged extension in real Chromium, pair it, execute ordinary tasks with actual model calls and verify pause/resume and outcomes. Never run CAMS or Karvy.
5. Fix failures and repeat affected tests. Stop on five occurrences of the same unresolved failure. Document all measured results, limitations, folder structure and launch steps.

The original project is read-only. No credentials, reports, logs or personal browser profiles are copied. No claim of equal live financial-site behavior without a later authorized financial-site test. API/model access is required; model providers may charge for real tests. Only this new companion folder is modified.
