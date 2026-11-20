import unittest
from unittest.mock import Mock, patch

from davosbot import tools, web_search


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class WebSearchToolTests(unittest.TestCase):
    def test_search_preserves_source_links_and_rejects_unsafe_urls(self):
        requests_module = Mock()
        requests_module.post.return_value = _FakeResponse({"results": [
            {"title": "Menu", "content": "Wings", "url": "https://restaurant.example/menu"},
            {"title": "Unsafe", "content": "Bad link", "url": "javascript:alert(1)"},
            {"title": "Credentials", "content": "Bad link", "url": "https://user:pass@example.test/"},
            {"title": "Empty username", "content": "Bad link", "url": "https://:pass@example.test/"},
            {"title": "Broken", "content": "Bad link", "url": "https://[invalid"},
        ]})
        reply = web_search._web_search("wings", api_key="test-key", requests_module=requests_module)
        self.assertIn("https://restaurant.example/menu", reply)
        self.assertNotIn("javascript:", reply)
        self.assertNotIn("user:pass", reply)
        self.assertNotIn(":pass@", reply)
        self.assertNotIn("[invalid", reply)

    def test_web_search_requires_api_key(self):
        post = Mock(side_effect=AssertionError("network should not run"))

        reply = web_search._web_search("Mariners news", api_key="", requests_module=Mock(post=post))

        self.assertEqual("No Tavily API key configured.", reply)
        post.assert_not_called()

    def test_web_search_formats_tavily_results(self):
        requests_module = Mock()
        requests_module.post.return_value = _FakeResponse({
            "results": [
                {"title": "One", "content": "First result"},
                {"title": "Two", "content": "Second result"},
            ]
        })

        reply = web_search._web_search("Mariners news", api_key="test-key", requests_module=requests_module)

        self.assertEqual("One\nFirst result\n\nTwo\nSecond result", reply)
        requests_module.post.assert_called_once_with(
            "https://api.tavily.com/search",
            json={"api_key": "test-key", "query": "Mariners news", "max_results": 5},
            timeout=15,
        )

    def test_web_search_handles_empty_results(self):
        requests_module = Mock()
        requests_module.post.return_value = _FakeResponse({"results": []})

        reply = web_search._web_search("nothing", api_key="test-key", requests_module=requests_module)

        self.assertEqual("No results found.", reply)

    def test_tools_facade_uses_tools_api_key_and_requests_module(self):
        fake_post = Mock(return_value=_FakeResponse({
            "results": [{"title": "Facade", "content": "Still wired"}],
        }))
        with (
            patch.object(tools, "TAVILY_API_KEY", "facade-key"),
            patch.object(tools.requests, "post", fake_post),
        ):
            reply = tools._web_search("live scores")

        self.assertEqual("Facade\nStill wired", reply)
        self.assertEqual("facade-key", fake_post.call_args.kwargs["json"]["api_key"])

    def test_execute_tool_uses_web_search_facade(self):
        with patch.object(tools, "_web_search", return_value="search ok") as search:
            reply = tools.execute_tool("web_search", {"query": "Mariners"}, sender="friend")

        self.assertEqual("search ok", reply)
        search.assert_called_once_with("Mariners")

    def test_live_info_tools_are_not_owner_only(self):
        self.assertNotIn("web_search", tools._OWNER_ONLY_TOOLS)
        self.assertNotIn("get_weather", tools._OWNER_ONLY_TOOLS)

    def test_tool_definitions_keep_search_and_weather_public_names(self):
        names = [tool["name"] for tool in tools.TOOL_DEFINITIONS]

        self.assertEqual(1, names.count("web_search"))
        self.assertEqual(1, names.count("get_weather"))
        search_def = next(tool for tool in tools.TOOL_DEFINITIONS if tool["name"] == "web_search")
        weather_def = next(tool for tool in tools.TOOL_DEFINITIONS if tool["name"] == "get_weather")
        self.assertEqual(["query"], search_def["parameters"]["required"])
        self.assertNotIn("required", weather_def["parameters"])


if __name__ == "__main__":
    unittest.main()
