import json
import subprocess
from pathlib import Path
from typing import Any, Dict, List

from dotenv import dotenv_values


ROOT = Path(__file__).resolve().parents[1]


def git(arguments: List[str]) -> bytes:
    return subprocess.check_output(["git", "-C", str(ROOT), *arguments])


def check() -> Dict[str, Any]:
    candidates = sorted(set(name for name in git(["ls-files", "--cached", "--others", "--exclude-standard", "-z"]).decode("utf-8").split("\0") if name))
    forbidden = [name for name in candidates if name == ".env" or (Path(name).name.startswith(".env.") and Path(name).name != ".env.example") or name.startswith((".venv/", "data/", "test-results/"))]
    if forbidden:
        raise RuntimeError("Private paths would be published: " + ", ".join(forbidden))
    if ".env.example" not in candidates:
        raise RuntimeError("Missing shareable environment template.")
    values = dotenv_values(ROOT / ".env")
    known_secrets = {name: value for name, value in values.items() if value and len(value) >= 8 and any(word in name.upper() for word in ("KEY", "PASSWORD", "SECRET", "TOKEN"))}
    matches: List[Dict[str, str]] = []
    for relative in candidates:
        path = ROOT / relative
        if path.is_symlink():
            raise RuntimeError("Review symlink before publishing: " + relative)
        if not path.is_file():
            continue
        data = path.read_bytes()
        for setting, value in known_secrets.items():
            if value.encode("utf-8") in data:
                matches.append({"file": relative, "setting": setting})
    if matches:
        raise RuntimeError("Local credential values found in candidate files: " + json.dumps(matches))
    required_ignored = [".env", ".env.local", "data/settings.enc", ".venv/pyvenv.cfg", "test-results/server.log"]
    for path in required_ignored:
        result = subprocess.run(["git", "-C", str(ROOT), "check-ignore", "--no-index", "-q", path], check=False)
        if result.returncode:
            raise RuntimeError("Private path is not ignored: " + path)
    return {"candidate_files": len(candidates), "local_secret_value_matches": 0, "private_paths_ignored": True, "staged_files": len(git(["diff", "--cached", "--name-only", "-z"]).split(b"\0")) - 1, "commits": len(git(["log", "--all", "--format=%H"]).splitlines()) if git(["for-each-ref", "--format=%(refname)"]).strip() else 0, "remotes_configured": bool(git(["remote"]).strip())}


if __name__ == "__main__":
    report = check()
    directory = ROOT / "test-results"
    directory.mkdir(exist_ok=True)
    (directory / "repository-check.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
