"""HTTP client for the current pting.club (Next.js) API."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, Optional

import requests


BASE_URL = "https://pting.club"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


class FengchaoApiError(RuntimeError):
    """Raised when the site cannot complete an API operation."""


@dataclass(frozen=True)
class SignInResult:
    """Normalized result returned to the MoviePilot plugin."""

    already_checked_in: bool
    message: str
    reward: int
    points: int
    current_streak: int
    max_streak: int
    date: str
    user: Dict[str, Any]
    surface: Dict[str, Any]


class FengchaoClient:
    """Small stateful client matching the site's browser request flow."""

    def __init__(
        self,
        username: str,
        password: str,
        proxies: Optional[Dict[str, str]] = None,
        timeout: int = 30,
        session: Optional[requests.Session] = None,
    ) -> None:
        self.username = username
        self.password = password
        self.proxies = proxies or {}
        self.timeout = max(5, int(timeout))
        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept": "application/json, text/plain, */*",
                "Origin": BASE_URL,
                "Referer": f"{BASE_URL}/login",
            }
        )
        if self.proxies:
            self.session.proxies.update(self.proxies)

    @staticmethod
    def _json(response: requests.Response, operation: str) -> Dict[str, Any]:
        try:
            payload = response.json()
        except (TypeError, ValueError) as exc:
            raise FengchaoApiError(
                f"{operation}返回了无法解析的数据（HTTP {response.status_code}）"
            ) from exc
        if not isinstance(payload, dict):
            raise FengchaoApiError(f"{operation}返回格式异常")
        return payload

    @staticmethod
    def _message(payload: Dict[str, Any], fallback: str) -> str:
        message = payload.get("message")
        return str(message).strip() if message else fallback

    def _request(self, method: str, path: str, **kwargs: Any) -> requests.Response:
        try:
            return self.session.request(
                method,
                f"{BASE_URL}{path}",
                timeout=self.timeout,
                **kwargs,
            )
        except requests.RequestException as exc:
            raise FengchaoApiError(f"访问蜂巢失败：{exc}") from exc

    def login(self) -> Dict[str, Any]:
        """Initialize CDN cookies, authenticate, and return the current user."""

        landing = self._request("GET", "/login")
        if landing.status_code != 200:
            raise FengchaoApiError(f"打开登录页失败（HTTP {landing.status_code}）")

        # The captcha mode is embedded in the Next.js RSC document. Do not keep
        # blindly retrying if the administrator enables an interactive challenge.
        mode_match = re.search(
            r"loginCaptchaMode.{0,32}?(OFF|TURNSTILE|BUILTIN|POW)",
            landing.text,
            re.IGNORECASE,
        )
        captcha_mode = mode_match.group(1).upper() if mode_match else "UNKNOWN"
        if captcha_mode not in {"OFF", "UNKNOWN"}:
            raise FengchaoApiError(
                f"站点已启用 {captcha_mode} 登录验证，当前无法无人值守登录"
            )

        response = self._request(
            "POST",
            "/api/auth/login",
            json={
                "login": self.username,
                "password": self.password,
                "loginMode": "password",
                "captchaToken": "",
                "builtinCaptchaCode": "",
                "powNonce": "",
                "addonFields": {},
            },
        )
        payload = self._json(response, "登录接口")
        if response.status_code != 200 or payload.get("code") != 0:
            raise FengchaoApiError(self._message(payload, "登录失败"))

        current = self.fetch_current_user()
        user = current.get("user")
        if not isinstance(user, dict) or not user.get("id"):
            raise FengchaoApiError("登录成功但未取得有效用户会话")
        return current

    def fetch_current_user(self) -> Dict[str, Any]:
        response = self._request(
            "GET",
            "/api/auth/me",
            headers={"Cache-Control": "no-store"},
        )
        payload = self._json(response, "用户状态接口")
        if response.status_code != 200 or payload.get("code") != 0:
            raise FengchaoApiError(self._message(payload, "获取用户状态失败"))
        data = payload.get("data")
        if not isinstance(data, dict):
            raise FengchaoApiError("用户状态返回格式异常")
        return data

    def check_in(self) -> SignInResult:
        """Login, skip an already-complete day, or perform today's check-in."""

        before = self.login()
        user = before.get("user") or {}
        surface = before.get("surface") or {}

        if surface.get("checkedInToday") is True:
            return SignInResult(
                already_checked_in=True,
                message="今日已签到",
                reward=0,
                points=int(surface.get("points") or user.get("points") or 0),
                current_streak=int(surface.get("currentCheckInStreak") or 0),
                max_streak=int(surface.get("maxCheckInStreak") or 0),
                date="",
                user=user,
                surface=surface,
            )

        response = self._request(
            "POST",
            "/api/check-in",
            json={"action": "check-in"},
        )
        payload = self._json(response, "签到接口")
        if response.status_code != 200 or payload.get("code") != 0:
            raise FengchaoApiError(self._message(payload, "签到失败"))

        result = payload.get("data")
        if not isinstance(result, dict):
            raise FengchaoApiError("签到接口返回格式异常")

        after = self.fetch_current_user()
        after_user = after.get("user") or user
        after_surface = after.get("surface") or surface
        if after_surface.get("checkedInToday") is not True:
            raise FengchaoApiError("签到接口返回成功，但用户状态未确认今日签到")

        return SignInResult(
            already_checked_in=bool(result.get("alreadyCheckedIn")),
            message=self._message(payload, "签到成功"),
            reward=int(result.get("reward") or 0),
            points=int(result.get("points") or after_surface.get("points") or 0),
            current_streak=int(
                result.get("currentStreak")
                or after_surface.get("currentCheckInStreak")
                or 0
            ),
            max_streak=int(
                result.get("maxStreak")
                or after_surface.get("maxCheckInStreak")
                or 0
            ),
            date=str(result.get("date") or ""),
            user=after_user,
            surface=after_surface,
        )
