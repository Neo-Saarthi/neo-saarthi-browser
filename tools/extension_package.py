import base64
import hashlib
import json
import shutil
import tempfile
import zipfile
from pathlib import Path
from typing import Optional


EXTENSION_ID = "polchmmnbnamnpkoiipldgdngmdllgbc"
PUBLIC_KEY = 'MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAvGB0KH+Z86eFChjbKoECg+6PIT4MpRu/2z1Xk+BezRToUc/d42ZmY8YtLwrIg1eza6QCHKi1fxzsayoDsBqgoqBGl5c+yBwrSeevrTRCMHyKo2e4JeLkXyA9GGxVdM8mPmUDilVvTbxNNJPSFuXecmQ7RGwOhImrtA/GO6njuesef9GEZNPkueH3H5cvJ/k+p02869CVfVPh3XQ4cbxYk0Dn/GAWBNFIY9uPEK3lshemCfRxKnHn/DOt5AZHlN8Znqh+dgd65TLdXBWQSHyookufkVnO6FTMjK03Zf2EaRf2gFs+aBJ46IYWo80U3Ovx9ObLb9x+SWctwuPjSFuRPwIDAQAB'


class ExtensionPackage:
    def __init__(self, source: str) -> None:
        self.source = Path(source).resolve()
        self.temporary: Optional[tempfile.TemporaryDirectory[str]] = None

    def __enter__(self) -> Path:
        self.temporary = tempfile.TemporaryDirectory(prefix="neo-extension-test-")
        destination = Path(self.temporary.name).resolve()
        try:
            if self.source.is_dir():
                shutil.copytree(self.source, destination, dirs_exist_ok=True)
            else:
                with zipfile.ZipFile(self.source) as archive:
                    for item in archive.infolist():
                        target = (destination / item.filename).resolve()
                        if not target.is_relative_to(destination):
                            raise ValueError("Unsafe extension archive path.")
                    archive.extractall(destination)
            manifest_path = destination / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest.setdefault("key", PUBLIC_KEY)
            digest = hashlib.sha256(base64.b64decode(manifest["key"])).hexdigest()[:32]
            identity = "".join(chr(ord("a") + int(character, 16)) for character in digest)
            if identity != EXTENSION_ID:
                raise ValueError("Extension public key does not match the trusted Store ID.")
            manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
            return destination
        except BaseException:
            self.temporary.cleanup()
            raise

    def __exit__(self, *args: object) -> None:
        if self.temporary:
            self.temporary.__exit__(*args)

