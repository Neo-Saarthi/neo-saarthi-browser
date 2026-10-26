# Gates: independent browser companion

Scope: working independent companion and original extension protocol, new UI, no app login, preserved engine, real nonfinancial end-to-end tests.

- [x] G1: Original source files remain unchanged and copied engine provenance is verified
  EVIDENCE: tools/verify_independence.py passed: 801 original files verified unchanged; 58 exact copied files including all 19 extension files; three documented source adapters.

- [x] G2: Companion runs from a separate directory using only its own files and dependencies
  EVIDENCE: Relocated clean source copy imported in a fresh isolated Python process (-I), with no original-project imports. Real tests started run.py on the existing fixed ports using the companion virtual environment.

- [x] G3: Pairing, origin restrictions, encrypted settings, persistence and task controls pass automated tests
  EVIDENCE: test-final.txt: 79 local tests passed in 8.848 seconds. Includes local-session/origin restrictions, exact extension ID, one-time pairing, encrypted settings, six-table storage, task exclusion and pause integration; copied calendar/input/continuation regressions passed.

- [x] G4: Real unchanged extension pairs and performs ordinary browser tasks with live model decisions and pause/resume
  EVIDENCE: test-results/live-report.json: passed=true. Real toolbar popup pairing and unchanged extension; native cursor enabled; actual JEV and DeepSeek HTTP 200 responses. Two note tasks completed in 16.0 and 17.3 seconds; second retained the same task ID across pause and resume, with no DOM changes during the pause. CAMS/Karvy were not visited.

- [x] G5: New UI is usable on desktop and mobile and documentation includes exact setup and run instructions
  EVIDENCE: README.md contains setup, folder structure, exact run commands, model-key requirements, pairing, credential controls and separate extension-pause semantics. Desktop and mobile screenshots were inspected; mobile overflow check passed in the real test.
