import importlib.util
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest
import requests

CLIENT_PATH = (
    Path(__file__).resolve().parents[1]
    / "plugins"
    / "fengchaonextsignin"
    / "client.py"
)
SPEC = importlib.util.spec_from_file_location("fengchao_client", CLIENT_PATH)
assert SPEC and SPEC.loader
CLIENT_MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = CLIENT_MODULE
SPEC.loader.exec_module(CLIENT_MODULE)

FengchaoApiError = CLIENT_MODULE.FengchaoApiError
FengchaoClient = CLIENT_MODULE.FengchaoClient


class FakeResponse:
    def __init__(self, status_code: int, payload: Dict[str, Any], text: str = ""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, responses: List[FakeResponse]):
        self.responses = list(responses)
        self.calls = []
        self.headers = {}
        self.proxies = {}

    def request(self, method, url, timeout, **kwargs):
        self.calls.append((method, url, timeout, kwargs))
        assert self.responses, f"unexpected request: {method} {url}"
        return self.responses.pop(0)


def user_payload(checked_in: bool, points: int = 10):
    return {
        "code": 0,
        "message": "success",
        "data": {
            "user": {"id": 42, "username": "tester", "points": points},
            "surface": {
                "checkedInToday": checked_in,
                "points": points,
                "currentCheckInStreak": 7,
                "maxCheckInStreak": 20,
            },
        },
    }


def test_successful_check_in_uses_new_api_and_verifies_state():
    session = FakeSession(
        [
            FakeResponse(200, {}, r'loginCaptchaMode\":\"OFF\"'),
            FakeResponse(200, {"code": 0, "message": "success"}),
            FakeResponse(200, user_payload(False)),
            FakeResponse(
                200,
                {
                    "code": 0,
                    "message": "签到成功，获得 3 积分",
                    "data": {
                        "points": 13,
                        "reward": 3,
                        "alreadyCheckedIn": False,
                        "date": "2026-08-17",
                        "currentStreak": 8,
                        "maxStreak": 20,
                    },
                },
            ),
            FakeResponse(200, user_payload(True, 13)),
        ]
    )
    client = FengchaoClient("user", "pass", session=session)

    result = client.check_in()

    assert result.reward == 3
    assert result.points == 13
    assert result.current_streak == 8
    assert result.already_checked_in is False
    assert [call[1].removeprefix("https://pting.club") for call in session.calls] == [
        "/login",
        "/api/auth/login",
        "/api/auth/me",
        "/api/check-in",
        "/api/auth/me",
    ]
    assert session.calls[3][3]["json"] == {"action": "check-in"}


def test_already_checked_in_does_not_post_again():
    session = FakeSession(
        [
            FakeResponse(200, {}, r'loginCaptchaMode\":\"OFF\"'),
            FakeResponse(200, {"code": 0, "message": "success"}),
            FakeResponse(200, user_payload(True)),
        ]
    )

    result = FengchaoClient("user", "pass", session=session).check_in()

    assert result.already_checked_in is True
    assert result.message == "今日已签到"
    assert len(session.calls) == 3


@pytest.mark.parametrize("mode", ["TURNSTILE", "BUILTIN", "POW"])
def test_interactive_captcha_stops_before_sending_credentials(mode):
    session = FakeSession(
        [FakeResponse(200, {}, f'loginCaptchaMode\\\":\\\"{mode}\\\"')]
    )

    with pytest.raises(FengchaoApiError, match=mode):
        FengchaoClient("user", "pass", session=session).login()

    assert len(session.calls) == 1


def test_login_error_message_is_preserved():
    session = FakeSession(
        [
            FakeResponse(200, {}, r'loginCaptchaMode\":\"OFF\"'),
            FakeResponse(401, {"code": 401, "message": "用户名或密码错误"}),
        ]
    )

    with pytest.raises(FengchaoApiError, match="用户名或密码错误"):
        FengchaoClient("user", "bad-pass", session=session).login()


def test_network_error_is_normalized():
    class BrokenSession(FakeSession):
        def request(self, method, url, timeout, **kwargs):
            raise requests.Timeout("timed out")

    with pytest.raises(FengchaoApiError, match="访问蜂巢失败"):
        FengchaoClient("user", "pass", session=BrokenSession([])).login()
