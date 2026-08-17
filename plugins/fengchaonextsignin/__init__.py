"""MoviePilot plugin for the current pting.club sign-in API."""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.core.config import settings
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import NotificationType

from .client import FengchaoApiError, FengchaoClient, SignInResult


class FengchaoNextSignin(_PluginBase):
    plugin_name = "蜂巢新版签到"
    plugin_desc = "适配蜂巢 Next.js 新版登录与签到接口。"
    plugin_icon = "https://raw.githubusercontent.com/lesir831/moviepilot-plugins/main/icons/fengchao.png"
    plugin_version = "1.0.1"
    plugin_author = "lesir831"
    author_url = "https://github.com/lesir831"
    plugin_config_prefix = "fengchaonextsignin_"
    plugin_order = 24
    auth_level = 2

    _enabled = False
    _notify = False
    _onlyonce = False
    _cron = "30 9 * * *"
    _username = ""
    _password = ""
    _use_proxy = True
    _timeout = 30
    _retry_count = 2
    _retry_delay = 10
    _history_days = 30

    _scheduler: Optional[BackgroundScheduler] = None
    _run_lock = threading.Lock()

    def init_plugin(self, config: dict = None):
        self.stop_service()
        config = config or {}

        self._enabled = bool(config.get("enabled", False))
        self._notify = bool(config.get("notify", False))
        self._onlyonce = bool(config.get("onlyonce", False))
        self._cron = str(config.get("cron") or "30 9 * * *").strip()
        self._username = str(config.get("username") or "").strip()
        self._password = str(config.get("user_password") or "")
        self._use_proxy = bool(config.get("use_proxy", True))
        self._timeout = self._bounded_int(config.get("timeout"), 30, 5, 120)
        self._retry_count = self._bounded_int(config.get("retry_count"), 2, 0, 5)
        self._retry_delay = self._bounded_int(config.get("retry_delay"), 10, 1, 300)
        self._history_days = self._bounded_int(config.get("history_days"), 30, 1, 365)

        if self._onlyonce:
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)
            self._scheduler.add_job(
                func=self.sign_in,
                trigger="date",
                run_date=datetime.now(tz=pytz.timezone(settings.TZ))
                + timedelta(seconds=3),
                name="蜂巢新版签到（立即运行）",
            )
            self._onlyonce = False
            self.update_config(self._config_dict())
            self._scheduler.start()
            logger.info("蜂巢新版签到：已安排立即运行")

    @staticmethod
    def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
        try:
            return max(minimum, min(maximum, int(value)))
        except (TypeError, ValueError):
            return default

    def _config_dict(self) -> Dict[str, Any]:
        return {
            "enabled": self._enabled,
            "notify": self._notify,
            "onlyonce": self._onlyonce,
            "cron": self._cron,
            "username": self._username,
            "user_password": self._password,
            "use_proxy": self._use_proxy,
            "timeout": self._timeout,
            "retry_count": self._retry_count,
            "retry_delay": self._retry_delay,
            "history_days": self._history_days,
        }

    def _proxies(self) -> Optional[Dict[str, str]]:
        if not self._use_proxy:
            return None
        proxy = getattr(settings, "PROXY", None)
        if proxy:
            return dict(proxy)
        logger.warning("蜂巢新版签到：已启用代理，但 MoviePilot 未配置系统代理")
        return None

    def sign_in(self):
        """Run one serialized sign-in attempt, with bounded immediate retries."""

        if not self._run_lock.acquire(blocking=False):
            logger.info("蜂巢新版签到：已有任务运行，本次跳过")
            return False

        try:
            if not self._username or not self._password:
                reason = "未配置蜂巢用户名或密码"
                logger.error(f"蜂巢新版签到：{reason}")
                self._record_failure(reason)
                self._send(False, reason)
                return False

            error = "未知错误"
            for attempt in range(self._retry_count + 1):
                if attempt:
                    logger.info(
                        f"蜂巢新版签到：第 {attempt}/{self._retry_count} 次重试"
                    )
                    time.sleep(self._retry_delay)

                try:
                    client = FengchaoClient(
                        username=self._username,
                        password=self._password,
                        proxies=self._proxies(),
                        timeout=self._timeout,
                    )
                    result = client.check_in()
                    self._record_success(result)
                    self._send(True, result.message, result)
                    logger.info(
                        f"蜂巢新版签到：{result.message}，奖励 {result.reward}，"
                        f"积分 {result.points}，连续 {result.current_streak} 天"
                    )
                    return True
                except FengchaoApiError as exc:
                    error = str(exc)
                    logger.warning(
                        f"蜂巢新版签到：第 {attempt + 1} 次尝试失败：{error}"
                    )
                except Exception as exc:
                    error = f"未预期错误：{exc}"
                    logger.exception("蜂巢新版签到执行异常")

            self._record_failure(error)
            self._send(False, error)
            logger.error(f"蜂巢新版签到最终失败：{error}")
            return False
        finally:
            self._run_lock.release()

    def _record_success(self, result: SignInResult) -> None:
        status = "已签到" if result.already_checked_in else "签到成功"
        record = {
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "status": status,
            "message": result.message,
            "reward": result.reward,
            "points": result.points,
            "current_streak": result.current_streak,
            "max_streak": result.max_streak,
        }
        self._append_history(record)
        self.save_data(
            "user_info",
            {
                "user": result.user,
                "surface": result.surface,
                "updated_at": record["time"],
            },
        )

    def _record_failure(self, reason: str) -> None:
        self._append_history(
            {
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "status": "签到失败",
                "message": reason,
                "reward": 0,
            }
        )

    def _append_history(self, record: Dict[str, Any]) -> None:
        history = self.get_data("history") or []
        if not isinstance(history, list):
            history = []
        history.append(record)
        cutoff = datetime.now() - timedelta(days=self._history_days)
        retained = []
        for item in history:
            try:
                item_time = datetime.strptime(item.get("time", ""), "%Y-%m-%d %H:%M:%S")
                if item_time >= cutoff:
                    retained.append(item)
            except (TypeError, ValueError):
                continue
        self.save_data("history", retained[-200:])

    def _send(
        self,
        success: bool,
        message: str,
        result: Optional[SignInResult] = None,
    ) -> None:
        if not self._notify:
            return
        if success and result:
            detail = (
                f"状态：{message}\n"
                f"奖励：{result.reward} 积分\n"
                f"当前积分：{result.points}\n"
                f"连续签到：{result.current_streak} 天"
            )
            title = "【蜂巢签到成功】"
        else:
            detail = f"状态：签到失败\n原因：{message}"
            title = "【蜂巢签到失败】"
        self.post_message(
            mtype=NotificationType.SiteMessage,
            title=title,
            text=f"{detail}\n时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        )

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        return []

    def get_service(self) -> List[Dict[str, Any]]:
        if not self._enabled or not self._cron:
            return []
        try:
            trigger = CronTrigger.from_crontab(self._cron)
        except (TypeError, ValueError) as exc:
            logger.error(f"蜂巢新版签到 cron 配置错误：{exc}")
            return []
        return [
            {
                "id": "FengchaoNextSignin",
                "name": "蜂巢新版签到服务",
                "trigger": trigger,
                "func": self.sign_in,
                "kwargs": {},
            }
        ]

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VAlert",
                        "props": {
                            "type": "info",
                            "variant": "tonal",
                            "text": "适配蜂巢 Next.js 新版 bbs_session 登录和 /api/check-in 接口；请停用旧版蜂巢签到插件。",
                            "class": "mb-4",
                        },
                    },
                    {
                        "component": "VRow",
                        "content": [
                            self._switch("enabled", "启用插件", 3),
                            self._switch("notify", "发送通知", 3),
                            self._switch("use_proxy", "使用系统代理", 3),
                            self._switch("onlyonce", "立即运行一次", 3),
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            self._field("username", "用户名 / 邮箱 / 手机号", 6),
                            self._field(
                                "user_password",
                                "密码",
                                6,
                                field_type="password",
                            ),
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            self._field("cron", "签到周期", 4, hint="5 位 cron，例如 30 9 * * *"),
                            self._field("timeout", "请求超时（秒）", 2, field_type="number"),
                            self._field("retry_count", "重试次数", 2, field_type="number"),
                            self._field("retry_delay", "重试间隔（秒）", 2, field_type="number"),
                            self._field("history_days", "历史天数", 2, field_type="number"),
                        ],
                    },
                ],
            }
        ], {
            "enabled": False,
            "notify": False,
            "use_proxy": True,
            "onlyonce": False,
            "cron": "30 9 * * *",
            "username": "",
            "user_password": "",
            "timeout": 30,
            "retry_count": 2,
            "retry_delay": 10,
            "history_days": 30,
        }

    @staticmethod
    def _switch(model: str, label: str, cols: int) -> Dict[str, Any]:
        return {
            "component": "VCol",
            "props": {"cols": 12, "md": cols},
            "content": [
                {
                    "component": "VSwitch",
                    "props": {"model": model, "label": label, "color": "primary"},
                }
            ],
        }

    @staticmethod
    def _field(
        model: str,
        label: str,
        cols: int,
        field_type: str = "text",
        hint: str = "",
    ) -> Dict[str, Any]:
        props = {
            "model": model,
            "label": label,
            "type": field_type,
            "clearable": field_type != "password",
        }
        if hint:
            props.update({"hint": hint, "persistent-hint": True})
        return {
            "component": "VCol",
            "props": {"cols": 12, "md": cols},
            "content": [{"component": "VTextField", "props": props}],
        }

    def get_page(self) -> List[dict]:
        user_info = self.get_data("user_info") or {}
        user = user_info.get("user") or {}
        surface = user_info.get("surface") or {}
        history = self.get_data("history") or []
        latest = history[-1] if history else {}

        summary = [
            f"用户：{user.get('displayName') or user.get('nickname') or user.get('username') or '暂无数据'}",
            f"今日签到：{'是' if surface.get('checkedInToday') else '否'}",
            f"当前积分：{surface.get('points', user.get('points', '-'))}",
            f"连续签到：{surface.get('currentCheckInStreak', '-')} 天",
            f"最近结果：{latest.get('status', '暂无记录')} {latest.get('message', '')}",
            f"更新时间：{user_info.get('updated_at') or latest.get('time') or '-'}",
        ]
        return [
            {
                "component": "VCard",
                "props": {"variant": "outlined"},
                "content": [
                    {"component": "VCardTitle", "text": "蜂巢新版签到"},
                    {
                        "component": "VCardText",
                        "content": [
                            {
                                "component": "div",
                                "props": {"class": "d-flex flex-column ga-2"},
                                "content": [
                                    {"component": "div", "text": line} for line in summary
                                ],
                            }
                        ],
                    },
                ],
            }
        ]

    def stop_service(self):
        if self._scheduler:
            try:
                if self._scheduler.running:
                    self._scheduler.shutdown(wait=False)
            except Exception as exc:
                logger.debug(f"停止蜂巢新版签到临时调度器时忽略异常：{exc}")
            finally:
                self._scheduler = None
