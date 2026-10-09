import unittest
from unittest.mock import Mock, patch

from davosbot import tools, weather


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class WeatherToolTests(unittest.TestCase):
    def test_geocode_defaults_to_belltown_without_network(self):
        with patch.object(weather.requests, "get", side_effect=AssertionError("network should not run")):
            result = weather._geocode_location("")

        self.assertEqual(("Belltown, Seattle, WA", 47.6132, -122.3454), result)

    def test_get_weather_formats_current_conditions(self):
        fake_get = Mock(return_value=_FakeResponse({
            "current": {
                "temperature_2m": 68.2,
                "apparent_temperature": 67.4,
                "precipitation": 0.02,
                "weather_code": 63,
                "wind_speed_10m": 8.2,
            }
        }))

        with patch.object(weather.requests, "get", fake_get):
            reply = weather._get_weather("Seattle")

        self.assertEqual(
            "Weather for Seattle, WA: 68F, feels 67F, rain, wind 8 mph, precip 0.02 in.",
            reply,
        )
        fake_get.assert_called_once()
        self.assertIn("api.open-meteo.com", fake_get.call_args.args[0])
        self.assertEqual(47.6062, fake_get.call_args.kwargs["params"]["latitude"])

    def test_get_weather_handles_geocode_miss(self):
        fake_get = Mock(return_value=_FakeResponse({"results": []}))

        with patch.object(weather.requests, "get", fake_get):
            reply = weather._get_weather("Nowhereville")

        self.assertIn("I couldn't resolve weather for 'Nowhereville'", reply)

    def test_execute_tool_uses_weather_facade(self):
        with patch.object(tools, "_get_weather", return_value="weather ok") as get_weather:
            reply = tools.execute_tool("get_weather", {"location": "Belltown"}, sender="friend")

        self.assertEqual("weather ok", reply)
        get_weather.assert_called_once_with("Belltown")


if __name__ == "__main__":
    unittest.main()
