import copy
import logging
from typing import Any, Dict

from uvicorn.config import LOGGING_CONFIG


class QuietBrowserPollingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        arguments = record.args
        if not isinstance(arguments, tuple) or len(arguments) < 5:
            return True
        path = str(arguments[2]).partition("?")[0]
        status_code = arguments[4]
        return not (
            status_code == 200
            and path in {
                "/api/v1/browser-use/bridge/next",
                "/api/v1/browser-use/status/desktop",
            }
        )


def uvicorn_log_config() -> Dict[str, Any]:
    config = copy.deepcopy(LOGGING_CONFIG)
    config.setdefault("filters", {})["quiet_browser_polling"] = {
        "()": QuietBrowserPollingFilter
    }
    config["handlers"]["access"]["filters"] = ["quiet_browser_polling"]
    return config
