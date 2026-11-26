import asyncio
import httpx
import pytest
from warden.notifier import discord_gateway as gw, discord as dc
from warden.notifier import discord_member_commands as members
from warden.notifier.discord_commands import COMMANDS

@pytest.fixture
def setup(config, monkeypatch):
    config.discord_owner_id = 'owner'
    config.discord_guild_id = 'guild'
    config.discord_member_role_id = 'plex-role'
    config.discord_channel_id = 'admin'
    seen = {}
    monkeypatch.setattr(dc, 'interaction_defer', lambda *a, **kw: seen.update(kw))
    monkeypatch.setattr(dc, 'interaction_respond', lambda c,t,text: seen.update(reply=text))
    monkeypatch.setattr(gw, 'dispatch', lambda *a: pytest.fail('Member reached admin dispatcher'))
    return config, seen

def interaction(command, roles=None, guild='guild', author='friend', channel='general'):
    return {'id':'i','token':'t','type':2,'guild_id':guild,'channel_id':channel,
            'member':{'user':{'id':author},'roles':roles or []},'data':{'name':command}}

@pytest.mark.parametrize('command', ['request','help','status'])
def test_members_get_private_safe_replies(setup,monkeypatch,command):
    config,seen=setup
    monkeypatch.setattr(members,'_reachable',lambda url:'Responding')
    members._cache.clear()
    asyncio.run(gw._handle_command(interaction(command,['plex-role']),config,None,None,None))
    assert seen['ephemeral'] is True
    assert 'owner' not in seen['reply']
    assert '127.0.0.1' not in seen['reply']

@pytest.mark.parametrize('command',['admin-status','diagnose','task','clear','user-stats','task-full'])
def test_members_cannot_run_admin_commands(setup,command):
    config,seen=setup
    asyncio.run(gw._handle_command(interaction(command,['plex-role']),config,None,None,None))
    assert 'owner' in seen['reply']

@pytest.mark.parametrize('guild,roles', [('guild',[]),('elsewhere',['plex-role']),(None,['plex-role'])])
def test_wrong_guild_dm_and_missing_role_denied(setup,guild,roles):
    config,seen=setup
    asyncio.run(gw._handle_command(interaction('request',roles,guild),config,None,None,None))
    assert 'Plex Member' in seen['reply']

def test_owner_admin_commands_need_private_channel(setup):
    config,seen=setup
    asyncio.run(gw._handle_command(interaction('diagnose',author='owner'),config,None,None,None))
    assert 'private #warden' in seen['reply']

def test_missing_owner_fails_closed(setup):
    config,seen=setup
    config.discord_owner_id=''
    asyncio.run(gw._handle_command(interaction('task'),config,None,None,None))
    assert 'owner' in seen['reply']

def test_status_failure_hides_exception_and_caches(config,monkeypatch):
    calls=[]
    def fail(url,**kw):
        calls.append(url)
        raise httpx.ConnectError('sensitive host and credentials')
    monkeypatch.setattr(members.httpx,'get',fail)
    members._cache.clear()
    result=members.member_reply('status',config)
    assert 'sensitive' not in result and 'Could not confirm' in result
    assert members.member_reply('status',config)==result
    assert len(calls)==2

def test_admin_commands_hidden_by_default():
    for command in COMMANDS:
        if command['name'] not in members.MEMBER_COMMANDS:
            assert command['default_member_permissions']=='0'

def test_ephemeral_defer_payload(monkeypatch):
    sent={}
    class Response:
        def raise_for_status(self): pass
    def post(url,**kw):
        sent.update(kw)
        return Response()
    monkeypatch.setattr(dc.httpx,'post',post)
    dc.interaction_defer('i','t',ephemeral=True)
    assert sent['json']=={'type':5,'data':{'flags':64}}
