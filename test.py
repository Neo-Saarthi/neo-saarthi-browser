import argparse
import os
import sys
import tempfile
import unittest
from pathlib import Path
from tools.extension_package import ExtensionPackage


ROOT = Path(__file__).resolve().parent
temporary = tempfile.TemporaryDirectory(prefix="neo-companion-tests-")
os.environ["COMPANION_DATA_DIR"] = temporary.name
os.environ["NEO_SAARTHI_NATIVE_BROWSER_CURSOR"] = "0"

from companion import runtime


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--extension-package", default="")
    arguments = parser.parse_args()
    package = ExtensionPackage(arguments.extension_package) if arguments.extension_package else None
    if package:
        os.environ["NEO_EXTENSION_TEST_DIR"] = str(package.__enter__())
    print(f"Testing standalone files: {runtime.ROOT}")
    result = unittest.TextTestRunner(verbosity=2).run(unittest.TestLoader().discover(str(ROOT / "tests"), pattern="test_*.py"))
    if package:
        package.__exit__()
    temporary.cleanup()
    sys.exit(0 if result.wasSuccessful() else 1)
