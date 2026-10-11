"""HDDolby session recovery. No MoviePilot imports or embedded credentials."""

import base64
import hashlib
import hmac
import struct
import time
from urllib.parse import parse_qs, urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup
from torrentool.api import Torrent


HOSTS = {"hddolby.com", "www.hddolby.com"}


class RecoveryError(RuntimeError):
    """Safe, credential-free error suitable for plugin logs."""


def trusted_url(url):
    p = urlparse(url)
    if (p.scheme not in {"http", "https"} or p.hostname not in HOSTS
            or p.username or p.password or p.port not in {None, 80, 443}):
        raise RecoveryError("拒绝向杜比站点以外的地址发送登录凭据")
    return urlunparse(p._replace(scheme="https", netloc=p.hostname, fragment=""))


def is_download(url):
    try:
        return urlparse(trusted_url(url)).path == "/download.php"
    except (RecoveryError, ValueError, TypeError):
        return False


def totp(secret, now=None):
    """SHA1 / 30 seconds / 6 digits, also accepts a matching otpauth URI."""
    secret = secret.strip()
    if secret.startswith("otpauth://"):
        p = urlparse(secret)
        q = parse_qs(p.query)
        if (p.netloc != "totp" or q.get("algorithm", ["SHA1"])[0].upper() != "SHA1"
                or q.get("period", ["30"])[0] != "30"
                or q.get("digits", ["6"])[0] != "6"):
            raise RecoveryError("验证器参数必须为 TOTP / SHA1 / 30 秒 / 6 位")
        secret = q.get("secret", [""])[0]
    try:
        secret = "".join(secret.split()).upper().rstrip("=")
        key = base64.b32decode(secret + "=" * (-len(secret) % 8))
        if len(key) < 10:
            raise ValueError()
    except Exception:
        raise RecoveryError("验证器密钥格式无效") from None
    counter = int(time.time() if now is None else now) // 30
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 15
    number = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7fffffff
    return f"{number % 1000000:06d}"


def twofa_form(response):
    if response is None or response.status_code != 200:
        return None
    if urlparse(response.url).hostname not in HOSTS:
        return None
    if b"<form" not in response.content.lower() or b"2fa" not in response.content.lower():
        return None
    soup = BeautifulSoup(response.content, "html.parser")
    return next((f for f in soup.find_all("form")
                 if f.find("input", attrs={"name": "2fa"})), None)


def response_kind(response):
    if response is None:
        return "network_error"
    if twofa_form(response) is not None:
        return "two_factor_required"
    head = response.content[:16384].lower()
    if (b"cf-chl-" in head or b"just a moment" in head
            or b"challenge-platform" in head):
        return "cloudflare_challenge"
    if urlparse(response.url).path in {"/login.php", "/takelogin.php"}:
        return "login_required"
    if response.status_code != 200:
        return f"http_{response.status_code}"
    return "unexpected_content"


def validate_torrent(response):
    if response is not None and response.status_code == 200:
        try:
            if response.content.startswith(b"d"):
                torrent = Torrent.from_string(response.content)
                if torrent.info_hash and torrent.files:
                    return response
        except Exception:
            pass
    raise RecoveryError("种子下载返回无效内容：" + response_kind(response))


class DolbySession:
    def __init__(self, site, *, secret_loader, before_verify=lambda: None,
                 session=None, timeout=20, proxies=None):
        self.site = site
        self.base = trusted_url(site["url"])
        self.secret_loader = secret_loader
        self.before_verify = before_verify
        self.session = session or requests.Session()
        # Same UA for account check, OTP submission and download (no Plugin suffix).
        self.session.headers["User-Agent"] = site["ua"]
        self.timeout = timeout
        self.proxies = proxies
        for part in (site.get("cookie") or "").split(";"):
            if "=" in part:
                name, value = part.strip().split("=", 1)
                self.session.cookies.set(name, value, domain=".hddolby.com", path="/")

    def close(self):
        self.session.close()

    def request(self, method, url, **kwargs):
        """Follow only trusted HTTPS redirects, including after POST."""
        for _ in range(6):
            url = trusted_url(url)
            try:
                response = self.session.request(method, url, allow_redirects=False,
                                                timeout=self.timeout,
                                                proxies=self.proxies, **kwargs)
            except requests.RequestException:
                raise RecoveryError("杜比请求失败或超时") from None
            if response.status_code not in {301, 302, 303, 307, 308}:
                return response
            location = response.headers.get("Location")
            if not location:
                return response
            url = trusted_url(urljoin(url, location))
            if response.status_code in {301, 302, 303}:
                method = "GET"
                kwargs.pop("data", None)
            response.close()
        raise RecoveryError("杜比重定向次数过多")

    def cookie_header(self):
        # A real prepared request resolves domain/path/expiry and deletion rules.
        request = self.session.prepare_request(requests.Request(
            "GET", urljoin(self.base, "download.php")))
        pairs = {}
        for part in request.headers.get("Cookie", "").split(";"):
            if "=" in part:
                k, v = part.strip().split("=", 1)
                pairs[k] = v
        return "; ".join(f"{k}={v}" for k, v in pairs.items())

    def ensure_authenticated(self, persist):
        response = self.request("GET", urljoin(self.base, "usercp.php"))
        form = twofa_form(response)
        recovered = False
        if form is not None:
            action = trusted_url(urljoin(response.url, form.get("action") or response.url))
            if urlparse(action).path != "/take2fa.php":
                raise RecoveryError("不支持的两步验证表单地址")
            payload = {i["name"]: i.get("value", "") for i in
                       form.find_all("input", attrs={"type": "hidden"}) if i.get("name")}
            code = totp(self.secret_loader())
            self.before_verify()
            payload["2fa"] = code
            posted = self.request("POST", action, data=payload,
                                  headers={"Referer": response.url})
            posted.close()
            response.close()
            # Site sometimes returns HTTP 200 + JS redirect. Never equate this to success.
            response = self.request("GET", urljoin(self.base, "usercp.php"))
            recovered = True
        soup = BeautifulSoup(response.content, "html.parser")
        valid = (response.status_code == 200
                 and urlparse(response.url).path == "/usercp.php"
                 and twofa_form(response) is None
                 and ("控制面板" in soup.get_text() or soup.select_one('a[href*="logout.php"]')))
        if not valid:
            kind = response_kind(response)
            response.close()
            raise RecoveryError("登录未确认：" + kind)
        response.close()
        cookie = self.cookie_header()
        if not cookie:
            raise RecoveryError("验证后未获得有效 Cookie")
        if recovered or cookie != self.site.get("cookie"):
            persist(cookie)
        return recovered

    def download(self, url, persist):
        if not is_download(url):
            raise RecoveryError("不支持的种子下载地址")
        response = self.request("GET", url)
        recovered = False
        if twofa_form(response) is not None:
            response.close()
            recovered = self.ensure_authenticated(persist)
            response = self.request("GET", url)  # exactly one retry after confirmed login
        try:
            return validate_torrent(response), recovered
        except RecoveryError:
            response.close()
            raise
