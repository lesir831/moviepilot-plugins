"""Protocol and failure-path regression tests; no live authentication."""

import importlib.util
from pathlib import Path
from unittest.mock import Mock

import pytest
import requests


spec = importlib.util.spec_from_file_location(
    "dolby_client", Path(__file__).parents[1] / "plugins/brushflow/site_auth.py")
client = importlib.util.module_from_spec(spec)
spec.loader.exec_module(client)

BASE = "https://www.hddolby.com/"
FORM = b'''<form method="post" action="take2fa.php"><input type="hidden" name="type" value="save">
<input type="hidden" name="returnto" value="index.php"><input name="2fa"></form>'''
ACCOUNT = '<html><title>控制面板</title><a href="logout.php">退出</a></html>'.encode()
TORRENT = b'd8:announce21:https://example.org/14:infod6:lengthi10e4:name4:test12:piece lengthi16384e6:pieces20:aaaaaaaaaaaaaaaaaaaaee'


def response(body, path="take2fa.php", status=200, headers=None):
    r = requests.Response()
    r.status_code = status
    r.url = BASE + path
    r._content = body
    r._content_consumed = True
    r.headers.update(headers or {})
    return r


def session_with(*responses, before_verify=lambda: None):
    s = requests.Session()
    s.request = Mock(side_effect=responses)
    loader = Mock(return_value="GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ")
    c = client.DolbySession({"url": BASE, "cookie": "uid=7; pass=old", "ua": "stable-UA"},
                            secret_loader=loader, before_verify=before_verify, session=s)
    return c, s, loader


def test_rfc6238_sha1_vectors():
    secret = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
    assert client.totp(secret, 59) == "287082"
    assert client.totp(secret, 1111111109) == "081804"
    assert client.totp(secret, 20000000000) == "353130"
    uri = f"otpauth://totp/test?secret={secret}&period=30&algorithm=SHA1&digits=6"
    assert client.totp(uri, 59) == "287082"


@pytest.mark.parametrize("secret", ["invalid!", "", "otpauth://hotp/test?secret=ABC",
    "otpauth://totp/test?secret=ABC&period=60"])
def test_bad_secrets_are_redacted(secret):
    with pytest.raises(client.RecoveryError):
        client.totp(secret)


def test_healthy_check_does_not_read_secret_or_submit_otp():
    c, s, loader = session_with(response(ACCOUNT, "usercp.php"))
    persist = Mock()
    assert c.ensure_authenticated(persist) is False
    assert s.request.call_count == 1
    loader.assert_not_called()
    persist.assert_not_called()


def test_200_js_response_requires_account_confirmation_and_persists_new_cookie():
    c, s, loader = session_with()
    calls = []
    def request(method, url, **kwargs):
        calls.append((method, kwargs))
        if len(calls) == 1:
            return response(FORM)
        if method == "POST":
            assert kwargs['data']['type'] == 'save'
            assert len(kwargs['data']['2fa']) == 6
            s.cookies.set('twofa', 'verified', domain='.hddolby.com', path='/')
            return response(b'<script>location.href="index.php"</script>')
        return response(ACCOUNT, 'usercp.php')
    s.request.side_effect = request
    persist = Mock()
    assert c.ensure_authenticated(persist) is True
    assert len(calls) == 3
    assert 'twofa=verified' in persist.call_args.args[0]
    assert s.headers['User-Agent'] == 'stable-UA'


def test_rejected_code_does_not_persist_cookie():
    c, s, _ = session_with(response(FORM), response(b'no'), response(FORM))
    persist = Mock()
    with pytest.raises(client.RecoveryError, match='two_factor_required'):
        c.ensure_authenticated(persist)
    persist.assert_not_called()
    assert s.request.call_count == 3


def test_download_recovers_and_retries_once():
    c, s, _ = session_with(response(FORM), response(FORM), response(b'ok'),
                            response(ACCOUNT, 'usercp.php'), response(TORRENT, 'download.php'))
    persist = Mock()
    r, recovered = c.download(BASE+'download.php?id=7&downhash=secret', persist)
    assert recovered and r.content == TORRENT
    persist.assert_called_once()
    assert s.request.call_count == 5


def test_second_challenge_does_not_loop():
    c, s, _ = session_with(response(FORM), response(FORM), response(b'ok'),
                            response(ACCOUNT, 'usercp.php'), response(FORM))
    with pytest.raises(client.RecoveryError, match='two_factor_required'):
        c.download(BASE+'download.php?id=7', Mock())
    assert s.request.call_count == 5


@pytest.mark.parametrize('body,status,kind', [
    (b'<title>Just a moment</title>', 403, 'cloudflare_challenge'),
    (b'<html>Error</html>', 200, 'unexpected_content'),
    (b'not a torrent', 502, 'http_502'),
])
def test_invalid_download_never_reaches_downloader(body, status, kind):
    c, s, loader = session_with(response(body, 'download.php', status))
    with pytest.raises(client.RecoveryError, match=kind):
        c.download(BASE+'download.php?id=7&passkey=TOPSECRET', Mock())
    loader.assert_not_called()
    assert s.request.call_count == 1


def test_cooldown_prevents_post():
    def deny():
        raise client.RecoveryError('cooldown')
    c, s, _ = session_with(response(FORM), before_verify=deny)
    with pytest.raises(client.RecoveryError, match='cooldown'):
        c.ensure_authenticated(Mock())
    assert s.request.call_count == 1


def test_external_redirect_does_not_receive_credentials():
    c, s, _ = session_with(response(b'', status=302, headers={'Location':'https://evil.example/login'}))
    with pytest.raises(client.RecoveryError):
        c.ensure_authenticated(Mock())
    assert s.request.call_count == 1


def test_external_form_action_is_rejected_before_otp():
    c, s, loader = session_with(response(FORM.replace(b'take2fa.php', b'https://evil.example/take2fa.php')))
    with pytest.raises(client.RecoveryError):
        c.ensure_authenticated(Mock())
    loader.assert_not_called()
    assert s.request.call_count == 1


def test_login_expiry_is_not_treated_as_2fa():
    c, s, loader = session_with(response(b'<form>login</form>', 'login.php'))
    with pytest.raises(client.RecoveryError, match='login_required'):
        c.ensure_authenticated(Mock())
    loader.assert_not_called()


def test_cookie_deletion_is_respected():
    c, s, _ = session_with()
    s.cookies.clear(domain='.hddolby.com', path='/', name='pass')
    assert c.cookie_header() == 'uid=7'


def test_network_errors_do_not_expose_download_keys():
    c, s, _ = session_with()
    s.request.side_effect = requests.ConnectionError('https://www.hddolby.com/?passkey=TOPSECRET')
    with pytest.raises(client.RecoveryError) as e:
        c.download(BASE+'download.php?passkey=TOPSECRET', Mock())
    assert 'TOPSECRET' not in str(e.value)


def test_scope_and_https_upgrade():
    assert client.is_download('http://www.hddolby.com/download.php?id=1')
    assert not client.is_download('https://hddolby.com.evil.example/download.php')
    assert not client.is_download(BASE+'usercp.php')
    assert client.trusted_url('http://www.hddolby.com/download.php').startswith('https://')
