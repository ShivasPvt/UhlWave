"""Weather and place tools, the AI's look-it-up loop, and sharing your location.
No real network calls: the weather service and the AI are faked."""
import asyncio
import json

import httpx
import pytest

from app import bot, messages, tools, ws
from tests.test_chat import receive, start_chat

GEOCODE = {
    "mandi": {"results": [{"name": "Mandi", "admin1": "Himachal Pradesh", "country": "India", "latitude": 31.71, "longitude": 76.93, "elevation": 760, "timezone": "Asia/Kolkata", "population": 26000}]},
    "kamand": {"results": [{"name": "Kamand", "admin1": "Himachal Pradesh", "country": "India", "latitude": 31.78, "longitude": 76.99}]},
}
FORECAST = {
    "timezone": "Asia/Kolkata",
    "current": {"time": "2026-10-09T11:30", "temperature_2m": 21.4, "apparent_temperature": 20.9, "relative_humidity_2m": 55,
                "weather_code": 2, "wind_speed_10m": 6.1, "precipitation": 0.0, "is_day": 1},
    "daily": {"time": ["2026-10-09"], "weather_code": [61], "temperature_2m_max": [24.0], "temperature_2m_min": [12.5],
              "precipitation_probability_max": [40], "sunrise": ["2026-10-09T06:21"], "sunset": ["2026-10-09T18:02"]},
}


@pytest.fixture
def fake_web(monkeypatch):
    calls = []

    async def fake_get_json(url, params):
        calls.append((url, params))
        if url == tools.GEOCODE_URL:
            return GEOCODE.get(params["name"].lower(), {})
        if url == tools.FORECAST_URL:
            return FORECAST
        raise AssertionError(url)

    monkeypatch.setattr(tools, "_get_json", fake_get_json)
    return calls


# ---------- the tools ----------

def test_weather_for_a_named_place(fake_web):
    async def go():
        w = await tools.get_weather(place="Mandi")
        assert w["place"] == "Mandi, Himachal Pradesh, India"
        assert w["now"]["conditions"] == "partly cloudy" and w["now"]["temperature_c"] == 21.4
        assert w["forecast"][0] == {"date": "2026-10-09", "conditions": "light rain", "max_c": 24.0, "min_c": 12.5,
                                    "chance_of_rain_percent": 40, "sunrise": "06:21", "sunset": "18:02"}
        forecast_params = fake_web[-1][1]
        assert (forecast_params["latitude"], forecast_params["longitude"]) == (31.71, 76.93)

    asyncio.run(go())


def test_weather_defaults_to_campus_and_accepts_coordinates(fake_web):
    async def go():
        w = await tools.get_weather()
        assert w["place"] == tools.AI_DEFAULT_PLACE
        w = await tools.get_weather(latitude=28.61, longitude=77.21, days=30)
        assert fake_web[-1][1]["forecast_days"] == 7  # capped

    asyncio.run(go())


def test_places_and_distance(fake_web):
    async def go():
        assert (await tools.find_place("Kamand"))[0]["region"] == "Himachal Pradesh"
        d = await tools.distance_between("Mandi", "Kamand")
        assert 5 < d["straight_line_km"] < 15
        assert round(tools.distance_km(28.6139, 77.2090, 19.0760, 72.8777)) in range(1140, 1160)  # Delhi to Mumbai

    asyncio.run(go())


def test_run_tool_reports_problems_as_json(fake_web):
    async def go():
        assert json.loads(await tools.run_tool("find_place", '{"query": "Atlantis"}'))["error"].startswith("Couldn't find")
        assert "no tool" in json.loads(await tools.run_tool("launch_rocket", "{}"))["error"]
        assert "valid JSON" in json.loads(await tools.run_tool("find_place", "{oops"))["error"]
        assert "Wrong arguments" in json.loads(await tools.run_tool("find_place", '{"city": "x"}'))["error"]
        ok = json.loads(await tools.run_tool("get_weather", '{"place": "Mandi"}'))
        assert ok["now"]["temperature_c"] == 21.4

    asyncio.run(go())


# ---------- the AI's look-it-up loop ----------

def chat_response(message: dict, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json={"choices": [{"message": message}]}, request=httpx.Request("POST", "http://x"))


def test_ai_calls_a_tool_then_answers(monkeypatch):
    async def go():
        monkeypatch.setattr(bot, "LLM_API_KEY", "test-key")
        sent = []
        replies = iter([
            chat_response({"content": None, "tool_calls": [{"id": "call_1", "type": "function",
                           "function": {"name": "get_weather", "arguments": '{"place": "Mandi"}'}}]}),
            chat_response({"content": "It's 21°C and partly cloudy in Mandi."}),
        ])

        async def fake_post(client, payload):
            sent.append(json.loads(json.dumps(payload)))
            return next(replies)

        async def fake_tool(name, arguments):
            return json.dumps({"place": "Mandi", "now": {"temperature_c": 21}})

        monkeypatch.setattr(bot, "_post", fake_post)
        monkeypatch.setattr(bot, "run_tool", fake_tool)
        reply = await bot.generate_reply([{"sender_is_bot": False, "sender_username": "riya", "content": "@ai weather in Mandi?"}])

        assert reply == "It's 21°C and partly cloudy in Mandi."
        assert sent[0]["tools"][0]["function"]["name"] == "get_weather"
        tool_message = sent[1]["messages"][-1]
        assert tool_message["role"] == "tool" and tool_message["tool_call_id"] == "call_1"

    asyncio.run(go())


def test_ai_works_without_tools_if_the_service_refuses_them(monkeypatch):
    async def go():
        monkeypatch.setattr(bot, "LLM_API_KEY", "test-key")
        sent = []
        replies = iter([
            httpx.Response(400, text='{"error": "tools are not supported for this model"}', request=httpx.Request("POST", "http://x")),
            chat_response({"content": "Hello!"}),
        ])

        async def fake_post(client, payload):
            sent.append(dict(payload))
            return next(replies)

        monkeypatch.setattr(bot, "_post", fake_post)
        assert await bot.generate_reply([{"sender_is_bot": False, "sender_username": "riya", "content": "hi"}]) == "Hello!"
        assert "tools" in sent[0] and "tools" not in sent[1]

    asyncio.run(go())


# ---------- sharing your location ----------

@pytest.fixture
def fake_places(monkeypatch):
    async def fake_reverse(lat, lon):
        return "Kamand, Mandi, Himachal Pradesh"
    monkeypatch.setattr(messages, "reverse_geocode", fake_reverse)


def test_share_location(client, make_user, fake_places):
    alice, bob, eve = make_user("alice"), make_user("bob"), make_user("eve")
    cid = start_chat(client, alice, bob["username"])["id"]
    url = f"/api/conversations/{cid}/location"

    with client.websocket_connect("/ws", headers=bob["headers"]) as ws_bob:
        r = client.post(url, json={"latitude": 31.7761234, "longitude": 76.9861234}, headers=alice["headers"])
        assert r.status_code == 201, r.text
        shared = receive(ws_bob, "message")["message"]
    assert shared["type"] == "location" and shared["content"] == "Kamand, Mandi, Himachal Pradesh"
    assert shared["location"]["latitude"] == 31.77612 and "openstreetmap.org" in shared["location"]["map_url"]

    assert client.post(url, json={"latitude": 123, "longitude": 0}, headers=alice["headers"]).status_code == 422
    assert client.post(url, json={"latitude": 1, "longitude": 1}, headers=eve["headers"]).status_code == 404

    # Deleting it removes the coordinates too.
    gone = client.delete(f"/api/messages/{shared['id']}", headers=alice["headers"]).json()
    assert gone["deleted"] is True and gone["location"] is None


def test_ai_sees_a_shared_location(client, make_user, fake_places, monkeypatch):
    seen = []

    async def fake_reply(history):
        seen.append(history)
        return "Sunny where you are."

    monkeypatch.setattr(ws, "generate_reply", fake_reply)
    alice = make_user("alice")
    cid = start_chat(client, alice, "ai")["id"]
    with client.websocket_connect("/ws", headers=alice["headers"]) as ws_alice:
        client.post(f"/api/conversations/{cid}/location", json={"latitude": 31.776, "longitude": 76.986}, headers=alice["headers"])
        receive(ws_alice, "message")
        assert receive(ws_alice, "message")["message"]["content"] == "Sunny where you are."
    assert seen[-1][-1]["content"] == "[shared location] Kamand, Mandi, Himachal Pradesh (latitude 31.776, longitude 76.986)"
