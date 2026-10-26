import json
import os
from typing import Any, Dict, List
from unittest.mock import patch

import httpx
import unittest

from core.jev import llm_decider, text, text_bridge
import text as legacy_text
from core.jev.logger import set_sensitive_values
from core.jev.model_config import browser_model_config
from core.jev.recipes import kfintech


class BrowserModelRoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.environment = patch.dict(os.environ, {"OPENCODE_API_KEY": "fixture-key"}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.addCleanup(set_sensitive_values, [])

    def test_global_model_cannot_redirect_browser(self) -> None:
        for override in ['muse-spark-1.3-contributor', 'deepseek-v4-pro', 'other-model']:
            with self.subTest(override=override):
                with patch.dict(os.environ, {"OPENCODE_MODEL": override, "TEXT_MODEL": override}):
                    for config in (browser_model_config, llm_decider._config, text._config):
                        assert config() == ("https://opencode.ai/zen/go/v1", "deepseek-v4.1-flash", "fixture-key")

    def test_insecure_endpoint_rejected(self) -> None:
        for url in ['http://example.test/v1', 'https://user:pass@example.test/v1', 'https://example.test/v1?token=x', 'https://example.test/v1#x', 'file:///tmp/model']:
            with self.subTest(url=url):
                with patch.dict(os.environ, {"BROWSER_MODEL_BASE_URL": url}):
                    with self.assertRaisesRegex(ValueError, "HTTPS"):
                        browser_model_config()

    def test_browser_endpoint_override_preserves_global_chat_settings(self) -> None:
        with patch.dict(os.environ, {"BROWSER_MODEL_BASE_URL": "https://gateway.test/v1/", "OPENCODE_MODEL": "muse-spark-1.3-contributor"}):
            assert browser_model_config() == ("https://gateway.test/v1", "deepseek-v4.1-flash", "fixture-key")
            assert os.environ["OPENCODE_MODEL"] == "muse-spark-1.3-contributor"

    def test_text_payload_redaction_and_route(self) -> None:
        for module in (text, legacy_text, text_bridge):
            with self.subTest(module_name=module.__name__):
                requests: List[httpx.Request] = []
                def handler(request: httpx.Request) -> httpx.Response:
                    requests.append(request)
                    return httpx.Response(200, json={"choices": [{"message": {"content": '{"text":"fixture"}'}}]})
                set_sensitive_values(["fixture-secret"])
                with httpx.Client(transport=httpx.MockTransport(handler)) as client, patch.object(module, "CLIENT", client):
                    assert module._ask_json("Return value", {"goal": "fixture-secret"}) == {"text": "fixture"}
                assert len(requests) == 1
                assert str(requests[0].url) == "https://opencode.ai/zen/go/v1/chat/completions"
                assert requests[0].headers["authorization"] == "Bearer fixture-key"
                assert requests[0].headers["x-opencode-session"]
                payload = json.loads(requests[0].content)
                assert payload["model"] == "deepseek-v4.1-flash"
                assert "fixture-secret" not in requests[0].content.decode()

    def test_vision_keeps_image_and_redacts_task(self) -> None:
        requests: List[Dict[str, Any]] = []
        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(json.loads(request.content))
            return httpx.Response(200, json={"choices": [{"message": {"content": '{"operation":"WAIT"}'}}]})
        messages = [{"role": "user", "content": [{"type": "text", "text": "fixture-secret"}, {"type": "image_url", "image_url": {"url": "data:image/png;base64,fixture"}}]}]
        set_sensitive_values(["fixture-secret"])
        with httpx.Client(transport=httpx.MockTransport(handler)) as client, patch.object(llm_decider, "CLIENT", client):
            assert llm_decider._call_llm(messages) == {"operation": "WAIT"}
        assert requests[0]["model"] == "deepseek-v4.1-flash"
        assert requests[0]["messages"][0]["content"][1] == messages[0]["content"][1]
        assert "fixture-secret" not in json.dumps(requests)

    def test_provider_failure_does_not_fall_back_to_another_model(self) -> None:
        requests: List[httpx.Request] = []
        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(503, json={"error": "fixture unavailable"})
        with httpx.Client(transport=httpx.MockTransport(handler)) as client, patch.object(llm_decider, "CLIENT", client):
            with self.assertRaises(httpx.HTTPStatusError):
                llm_decider._call_llm([{"role": "user", "content": "fixture"}])
        assert len(requests) == 1
        assert json.loads(requests[0].content)["model"] == "deepseek-v4.1-flash"

    def test_portal_image_reader_uses_same_deepseek_route(self) -> None:
        requests: List[httpx.Request] = []
        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json={"choices": [{"message": {"content": "A1B2C"}}]})
        client = httpx.Client(transport=httpx.MockTransport(handler))
        with patch.object(kfintech.httpx, "Client", return_value=client):
            assert kfintech._solve_captcha_image("data:image/png;base64,fixture") == "A1B2C"
        assert len(requests) == 1
        payload = json.loads(requests[0].content)
        assert payload["model"] == "deepseek-v4.1-flash"
        assert payload["messages"][0]["content"][1]["image_url"]["url"] == "data:image/png;base64,fixture"

    def test_missing_provider_key_fails_without_using_unrelated_openai_key(self) -> None:
        with patch.dict(os.environ, {"OPENAI_API_KEY": "unrelated"}, clear=True), patch("core.jev.model_config.Path.is_file", return_value=False):
            with self.assertRaisesRegex(ValueError, "OPENCODE_API_KEY"):
                browser_model_config()

