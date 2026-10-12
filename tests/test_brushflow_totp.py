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
    assert q.add_torrent.call_args.kwargs['tag'] == ['已整理', '刷流']
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
    p._content_info_hash = Mock(return_value='old-hash')
    q.get_torrents.return_value = ([{'hash':'old-hash'}],False)
    assert p._BrushFlow__download(t) == 'old-hash'
    q.get_torrents.assert_called_once_with(ids=['old-hash'])
    q.get_torrent_id_by_tag.assert_not_called()


def test_transmission_uses_same_recovered_bytes(download_env):
    p,q,helper,t,get = download_env
    helper.is_downloader.side_effect = lambda kind,**kw: kind=='transmission'
    q.add_torrent.return_value = SimpleNamespace(hashString='tr-hash')
    assert p._BrushFlow__download(t) == 'tr-hash'
    assert q.add_torrent.call_args.kwargs['labels'] == ['已整理', '刷流']
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


def test_old_qb_does_not_accept_unconfirmed_hash(download_env, monkeypatch):
    p,q,_,t,_ = download_env
    q.add_torrent.return_value = (True,[])
    p._content_info_hash = Mock(return_value='missing')
    q.get_torrents.return_value = ([{'hash':'unrelated'}],False)
    monkeypatch.setattr(module.time,'sleep',lambda _:None)
    assert p._BrushFlow__download(t) is None
    assert q.get_torrents.call_count == 5
    q.get_torrent_id_by_tag.assert_not_called()


def test_hash_resolution_from_file_and_magnets():
    import base64
    content = b'd8:announce21:https://example.org/14:infod6:lengthi10e4:name4:test12:piece lengthi16384e6:pieces20:aaaaaaaaaaaaaaaaaaaaee'
    expected = module.Torrent.from_string(content).info_hash.lower()
    resolve = module.BrushFlow._content_info_hash
    assert resolve(content) == expected
    assert resolve('magnet:?xt=urn:btih:'+expected.upper()) == expected
    encoded = base64.b32encode(bytes.fromhex(expected)).decode()
    assert resolve('magnet:?xt=urn:btih:'+encoded) == expected
    assert resolve(b'<html>not torrent</html>') is None
    assert resolve('https://example.org/download.php') is None


def test_internal_ownership_prevents_other_task_adoption(monkeypatch):
    p = object.__new__(module.BrushFlow)
    first, second, other = task(id='first'),task(id='second'),task(id='other',downloader='other-qb')
    p._task_configs = {t.id:t for t in (first,second,other)}
    p._BrushFlow__get_hash = Mock(return_value='hash')
    p._BrushFlow__get_label = Mock(return_value=['刷流'])
    p._BrushFlow__get_site_by_torrent = Mock(return_value=(26,'杜比'))
    p._get_task_data = Mock(side_effect=lambda tid,key: {'hash':{}} if tid=='second' else {})
    assert not p._is_primary_task_for_torrent(first,{})
    assert p._is_primary_task_for_torrent(second,{})
    assert p._is_primary_task_for_torrent(other,{})  # independent downloader
    p._get_task_data = Mock(return_value={})
    assert p._is_primary_task_for_torrent(first,{})
    assert not p._is_primary_task_for_torrent(second,{})


def test_downloading_count_uses_task_hashes(download_env):
    p,q,_,_,_ = download_env
    p._get_task_data = Mock(return_value={'a':{},'b':{},'deleted':{'deleted':True}})
    q.get_downloading_torrents.return_value = [{'hash':'a'},{'hash':'foreign'}]
    assert p._BrushFlow__get_downloading_count() == 1
    q.get_downloading_torrents.assert_called_once_with(ids=['a','b'],tags='刷流')
    p._get_task_data.return_value = {}
    q.get_downloading_torrents.reset_mock()
    assert p._BrushFlow__get_downloading_count() == 0
    q.get_downloading_torrents.assert_not_called()


def test_removing_unique_tag_keeps_existing_ownership(download_env):
    p,q,_,_,_ = download_env
    p._save_current_task_data = Mock()
    p._BrushFlow__log_and_send_torrent_task_update_message = Mock()
    rows = {'a':{'deleted':False}}
    unmanaged = {}
    p._BrushFlow__update_seeding_tasks_based_on_tags(rows,unmanaged,{'a':{'hash':'a','tags':'刷流, 已整理'}})
    assert 'a' in rows and not unmanaged
    p._BrushFlow__update_seeding_tasks_based_on_tags(rows,unmanaged,{'a':{'hash':'a','tags':'已整理'}})
    assert 'a' not in rows and 'a' in unmanaged
