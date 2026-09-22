import json

import httpx
import pytest

from church_bot.errors import ConfigError, MessengerError
from church_bot.messengers.line import LineMessenger
from church_bot.models import OutgoingMessage
from tests.conftest import gid, uid


def make(handler):
    client = httpx.Client(base_url="https://api.line.me", transport=httpx.MockTransport(handler))
    return LineMessenger("secret-token", client=client, sleep=lambda _: None)


def test_push_payload_and_headers():
    seen: list[httpx.Request] = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={})

    make(handler).send(gid(), OutgoingMessage("哈囉"))
    request = seen[0]
    assert request.url.path == "/v2/bot/message/push"
    assert json.loads(request.content) == {"to": gid(), "messages": [{"type": "text", "text": "哈囉"}]}
    assert request.headers["authorization"] == "Bearer secret-token"
    assert len(request.headers["x-line-retry-key"]) == 36


def test_server_error_is_retried_with_same_retry_key():
    keys: list[str] = []

    def handler(request):
        keys.append(request.headers["x-line-retry-key"])
        return httpx.Response(500 if len(keys) == 1 else 200, json={})

    make(handler).send(gid(), OutgoingMessage("hi"))
    assert len(keys) == 2 and keys[0] == keys[1]


def test_conflict_means_already_accepted():
    make(lambda r: httpx.Response(409, json={"message": "already accepted"})).send(gid(), OutgoingMessage("hi"))


def test_monthly_limit_is_not_retried_and_explained():
    calls: list[int] = []

    def handler(request):
        calls.append(1)
        return httpx.Response(429, json={"message": "You have reached your monthly limit."})

    with pytest.raises(MessengerError) as exc:
        make(handler).send(gid(), OutgoingMessage("hi"))
    assert len(calls) == 1 and not exc.value.retryable
    assert "額度" in exc.value.message and "LINE_PRICING" in exc.value.hint


def test_bad_token_is_explained():
    with pytest.raises(MessengerError) as exc:
        make(lambda r: httpx.Response(401, json={"message": "Authentication failed"})).check()
    assert "token" in exc.value.message and exc.value.status_code == 401


def test_rejected_mention_falls_back_to_plain_text():
    bodies: list[dict] = []

    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        is_v2 = body["messages"][0]["type"] == "textV2"
        return httpx.Response(400 if is_v2 else 200, json={"message": "invalid"})

    result = make(handler).send(gid(), OutgoingMessage("陳小明", "{m0}", (("m0", uid()),)))
    assert bodies[0]["messages"][0]["substitution"]["m0"]["mentionee"] == {"type": "user", "userId": uid()}
    assert bodies[1]["messages"] == [{"type": "text", "text": "陳小明"}]
    assert "純文字" in result.note


def test_quota_and_audience_size():
    def handler(request):
        return {
            "/v2/bot/message/quota": httpx.Response(200, json={"type": "limited", "value": 200}),
            "/v2/bot/message/quota/consumption": httpx.Response(200, json={"totalUsage": 50}),
            f"/v2/bot/group/{gid()}/members/count": httpx.Response(200, json={"count": 30}),
        }[request.url.path]

    messenger = make(handler)
    quota = messenger.quota()
    assert (quota.limit, quota.used, quota.remaining) == (200, 50, 150)
    assert messenger.audience_size(gid()) == 30 and messenger.audience_size(uid()) == 1


def test_missing_token_is_a_config_error():
    with pytest.raises(ConfigError):
        LineMessenger("")


def test_reply_with_mentions_uses_text_v2_and_falls_back_to_plain_text():
    bodies: list[dict] = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(400 if len(bodies) == 1 else 200, json={"message": "bad"})

    tagged = OutgoingMessage("司琴：小明", mention_text="司琴：{m0}", mentions=(("m0", uid()),))
    make(handler).reply_texts("tok", [tagged, "✅ 已免費送出"])
    first, second = (b["messages"] for b in bodies)
    assert first[0]["type"] == "textV2" and first[0]["substitution"]["m0"]["mentionee"]["userId"] == uid()
    assert first[1] == {"type": "text", "text": "✅ 已免費送出"}
    assert [m["type"] for m in second] == ["text", "text"] and second[0]["text"] == "司琴：小明"
