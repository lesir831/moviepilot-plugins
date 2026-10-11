"""Fork integration tests. No live downloads or MoviePilot configuration writes."""
import importlib.util
import sys
import threading
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

pytest.importorskip('app.core.config')
ROOT = Path(__file__).parents[1] / 'plugins/brushflow'
spec = importlib.util.spec_from_file_location('brushflow_fork_test', ROOT / '__init__.py',
                                             submodule_search_locations=[str(ROOT)])
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
SECRET = 'GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ'  # public RFC test vector


def task(**kwargs):
    return module.BrushTaskConfig({'id':'task1','name':'杜比','site_id':26,
                                   'downloader':'qb','totp_secret':SECRET, **kwargs})


def payload(**kwargs):
    return module.BrushTaskPayload(name='杜比',site_id=26,downloader='qb',**kwargs)


def test_secret_roundtrip_is_private():
    t = task()
    assert t.to_dict()['totp_secret'] == SECRET
    assert 'totp_secret' not in t.to_public_dict()
    assert t.to_public_dict()['totp_configured']
    assert SECRET not in repr(payload(totp_secret=SECRET))


def test_blank_preserves_explicit_clear_removes_and_site_change_drops_secret():
    old = task()
    assert payload().task_config(old)['totp_secret'] == SECRET
    assert payload(totp_secret='').task_config(old)['totp_secret'] == SECRET
    assert payload(clear_totp=True).task_config(old)['totp_secret'] is None
    p = payload()
    p.site_id = 99
    assert p.task_config(old)['totp_secret'] is None
    assert payload().task_config()['totp_secret'] is None


def test_invalid_secret_rejected():
    with pytest.raises(ValueError):
        payload(totp_secret='123456')
    uri = f'otpauth://totp/test?secret={SECRET}&period=30&algorithm=SHA1&digits=6'
    assert payload(totp_secret=uri).task_config()['totp_secret'] == uri


@pytest.fixture
def download_env(monkeypatch):
    p = object.__new__(module.BrushFlow)
    p._get_task_config = Mock(return_value=task())
    p._download_dolby = Mock(return_value=b'torrent-bytes')
    q = Mock()
    service = SimpleNamespace(instance=q)
    monkeypatch.setattr(module.BrushFlow,'downloader',property(lambda self:q))
    monkeypatch.setattr(module.BrushFlow,'service_info',property(lambda self:service))
    helper = Mock()
    helper.is_downloader.side_effect = lambda kind,**kw: kind=='qbittorrent'
    monkeypatch.setattr(module,'DownloaderHelper',Mock(return_value=helper))
    get = Mock(side_effect=AssertionError('unexpected legacy fetch'))
    monkeypatch.setattr(module.RequestUtils,'get_res',get)
    torrent = SimpleNamespace(enclosure='https://www.hddolby.com/download.php?id=7',
                              site_proxy=False,site_cookie='old',site_ua='old-UA',site=26,title='sample')
    return p,q,helper,torrent,get


def test_dolby_bytes_reach_qb_once_without_legacy_fetch(download_env):
    p,q,helper,t,get = download_env
    q.add_torrent.return_value = (True,['infohash'])
    assert p._BrushFlow__download(t) == 'infohash'
    assert q.add_torrent.call_args.kwargs['content'] == b'torrent-bytes'
    assert q.add_torrent.call_args.kwargs['cookie'] is None
    q.get_torrent_id_by_tag.assert_not_called()
    get.assert_not_called()


def test_qb_failure_tuple_does_not_poll_hash(download_env):
    p,q,_,t,_ = download_env
    q.add_torrent.return_value = (False,[])
    assert p._BrushFlow__download(t) is None
    q.get_torrent_id_by_tag.assert_not_called()


def test_old_qb_boolean_response_supported(download_env):
    p,q,_,t,_ = download_env
    q.add_torrent.return_value = True
    q.get_torrent_id_by_tag.return_value = 'old-hash'
    assert p._BrushFlow__download(t) == 'old-hash'


def test_transmission_uses_same_recovered_bytes(download_env):
    p,q,helper,t,get = download_env
    helper.is_downloader.side_effect = lambda kind,**kw: kind=='transmission'
    q.add_torrent.return_value = SimpleNamespace(hashString='tr-hash')
    assert p._BrushFlow__download(t) == 'tr-hash'
    assert q.add_torrent.call_args.kwargs['content'] == b'torrent-bytes'
    get.assert_not_called()


def test_auth_failure_never_calls_downloader(download_env):
    p,q,_,t,_ = download_env
    p._download_dolby.side_effect = module.RecoveryError('登录未确认：two_factor_required')
    with pytest.raises(module.RecoveryError):
        p._BrushFlow__download(t)
    q.add_torrent.assert_not_called()


def test_other_sites_keep_original_route(download_env):
    p,q,_,t,get = download_env
    t.enclosure = 'https://another.example/download.php?id=7'
    get.side_effect = None
    get.return_value = SimpleNamespace(ok=True,content=b'other-torrent')
    q.add_torrent.return_value = (True,['other-hash'])
    assert p._BrushFlow__download(t) == 'other-hash'
    p._download_dolby.assert_not_called()
    get.assert_called_once()


def test_recovery_updates_site_cookie_and_notifies(monkeypatch):
    p = object.__new__(module.BrushFlow)
    p._totp_attempts = {}
    p.eventmanager = Mock()
    site = SimpleNamespace(id=26,domain='hddolby.com',name='杜比',cookie='old',
                           url='https://www.hddolby.com/',ua='stable-UA',proxy=False)
    oper = Mock()
    oper.get.return_value = site
    oper.update.side_effect = lambda sid,updates:setattr(site,'cookie',updates['cookie'])
    monkeypatch.setattr(module,'SiteOper',Mock(return_value=oper))
    response = SimpleNamespace(content=b'valid',close=Mock())
    def make_client(config,secret_loader,before_verify,proxies):
        assert secret_loader() == SECRET
        assert config['ua']=='stable-UA'
        def download(url,persist):
            before_verify()
            persist('verified-cookie')
            return response,True
        return SimpleNamespace(download=download,close=Mock())
    monkeypatch.setattr(module,'DolbySession',make_client)
    assert p._download_dolby(task(),'https://www.hddolby.com/download.php?id=7') == b'valid'
    assert site.cookie == 'verified-cookie'
    p.eventmanager.send_event.assert_called_once()
    with pytest.raises(module.RecoveryError,match='冷却'):
        p._download_dolby(task(),'https://www.hddolby.com/download.php?id=7')


def test_download_failure_run_is_not_marked_success():
    p = object.__new__(module.BrushFlow)
    p._get_task_config = Mock(return_value=task())
    p.get_state = lambda:True
    p._task_locks = {}
    p._brush_lock = threading.Lock()
    p._task_scope = lambda tid:nullcontext()
    p._set_runtime = Mock()
    p._append_run = Mock()
    p._run_brush = lambda t,r:r.update(result='download_failed',error='two_factor_required')
    p.brush('task1')
    assert p._append_run.call_args.args[1]['success'] is False


def test_failure_reason_saved_without_discarding_existing_additions(monkeypatch):
    from collections import Counter
    p = object.__new__(module.BrushFlow)
    p._get_task_config = Mock(return_value=task(except_subscribe=False))
    p._BrushFlow__calculate_seeding_torrents_size = lambda records:0
    p._BrushFlow__evaluate_pre_conditions_for_brush = lambda **kw:(True,None)
    p._BrushFlow__evaluate_size_condition_for_brush = lambda *a,**kw:(True,None)
    p._BrushFlow__evaluate_conditions_for_brush = lambda *a:(True,None)
    p._BrushFlow__download = Mock(side_effect=module.RecoveryError('登录未确认：two_factor_required'))
    chain = Mock()
    chain.browse.return_value = [SimpleNamespace(pubdate='2026-10-11 18:00:00',size=1)]
    monkeypatch.setattr(module,'TorrentsChain',Mock(return_value=chain))
    report={'reason_counts':Counter(),'added_count':0}
    records={'existing':{'size':42}}
    p._BrushFlow__brush_site_torrents(SimpleNamespace(name='杜比',domain='hddolby.com'),records,{},set(),report,0)
    assert report['result']=='download_failed'
    assert report['reason_counts']['登录未确认：two_factor_required']==1
    assert records['existing']['size']==42
