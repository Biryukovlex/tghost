"""Synthetic OAuth, local-only inference and owner-scoped provider regressions."""
import asyncio
import base64
import json
import re
import time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from pydantic_ai import Agent
from pydantic_ai.providers.openai import OpenAIProvider

from app.config import Settings
from app.session_crypto import build_cipher
from app.studio import connections as c
from app.studio.model import ChatGPTPlanModel, StudioConfigurationError, build_model, model_configured, model_name
from app.workspace_settings import RuntimeSettings, WorkspaceSettings


def record(**extra):
    return dict(client_id='issued-client', subject='synthetic-account', access_token='synthetic-access',
                refresh_token='synthetic-refresh', scopes=['resource.invoke', 'chatgpt.tokens.use.direct'],
                expires_at=time.time() + 3600, **extra)


class Store:
    def __init__(self):
        self._db = SimpleNamespace(workspace_id='synthetic-workspace')
        self._cipher = object()
        self.rows = {}
        self.base = Settings(studio_provider='openai', openai_model='synthetic-model')
    @property
    def effective(self):
        return RuntimeSettings(self.base, overlay=self.rows)
    async def set(self, key, value):
        self.rows[key] = value
    async def reset(self, key):
        self.rows.pop(key, None)


@pytest.fixture(autouse=True)
def empty_pending():
    c._PENDING.clear()
    c._LOCKS.clear()
    yield
    c._PENDING.clear()
    c._LOCKS.clear()


@pytest.mark.parametrize('url', ['https://example.org', 'http://169.254.169.254', 'http://localhost:80',
    'http://user:password@localhost:11434', 'http://localhost:11434/other', 'http://localhost:11434?x=1'])
def test_ollama_disallows_nonlocal_or_ambiguous_origins(url):
    with pytest.raises(ValueError):
        c.ollama_origin(url)


def test_provider_construction_does_not_require_openrouter_key():
    local = Settings(studio_provider='ollama', ollama_model='local-model')
    remote = Settings(studio_provider='openai', openai_model='plan-model', studio_openai_oauth=json.dumps(record()))
    assert model_configured(local) and model_configured(remote)
    assert model_name(local) == 'local-model'
    assert build_model(local).model_name == 'local-model'
    assert isinstance(build_model(remote), ChatGPTPlanModel)
    assert not model_configured(Settings(studio_provider='openai', openai_model='plan-model'))
    assert 'synthetic-access' not in repr(remote)


@pytest.mark.asyncio
async def test_identity_signature_audience_nonce_and_account(monkeypatch):
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    def enc(value):
        if not isinstance(value, bytes):
            value = json.dumps(value).encode()
        return base64.urlsafe_b64encode(value).decode().rstrip('=')
    numbers = private.public_key().public_numbers()
    key = {'kid': 'synthetic-key', 'kty': 'RSA', 'alg': 'RS256',
        'n': enc(numbers.n.to_bytes(256, 'big')), 'e': enc(numbers.e.to_bytes(3, 'big'))}
    async def keys(request):
        assert str(request.url) == c.AUTH_ORIGIN + '/.well-known/jwks.json'
        return httpx.Response(200, json={'keys': [key]})
    real = httpx.AsyncClient
    monkeypatch.setattr(c.httpx, 'AsyncClient', lambda **kwargs: real(transport=httpx.MockTransport(keys), **kwargs))
    claims = {'iss': c.AUTH_ORIGIN, 'aud': 'issued-client', 'sub': 'account', 'nonce': 'nonce', 'exp': time.time() + 600}
    def jwt(changes):
        body = enc({'alg': 'RS256', 'kid': 'synthetic-key'}) + '.' + enc({**claims, **changes})
        return body + '.' + enc(private.sign(body.encode(), padding.PKCS1v15(), hashes.SHA256()))
    assert (await c.validate_identity(jwt({}), 'issued-client', nonce='nonce', subject='account'))['sub'] == 'account'
    for changes in [{'iss': 'https://attacker.invalid'}, {'aud': 'other-client'}, {'nonce': 'other'},
                    {'sub': 'other'}, {'exp': 0}, {'nbf': time.time()+1000}, {'aud': ['issued-client','other']}]:
        with pytest.raises(ValueError):
            await c.validate_identity(jwt(changes), 'issued-client', nonce='nonce', subject='account')
    with pytest.raises(Exception):
        await c.validate_identity(jwt({})[:-10]+'AAAAAAAAAA', 'issued-client')


@pytest.mark.asyncio
async def test_dynamic_registration_pkce_and_one_use(monkeypatch):
    store = Store()
    first = parse_qs(urlsplit(await c.start_sign_in(store, workspace_id='w', user_id='u', callback='http://127.0.0.1:8080/auth/callback')).query)
    second = parse_qs(urlsplit(await c.start_sign_in(store, workspace_id='w', user_id='u', callback='http://127.0.0.1:8080/auth/callback')).query)
    assert first['client_id'] == ['dynamic_agent_client']
    assert first['ext_agent_host_id'] == second['ext_agent_host_id']
    assert first['state'] != second['state']
    assert first['code_challenge_method'] == ['S256']
    seen = []
    async def token(data):
        seen.append(data)
        return {'access_token': 'access', 'refresh_token': 'refresh', 'id_token': 'id', 'scope': c.SCOPES, 'expires_in': 3600}
    async def identity(token, client_id, **kwargs):
        assert kwargs['nonce'] == first['nonce'][0]
        return {'sub': 'synthetic-account'}
    monkeypatch.setattr(c, '_token', token)
    monkeypatch.setattr(c, 'validate_identity', identity)
    query = {'state': first['state'][0], 'code': 'synthetic-code', 'client_id': 'issued-client'}
    with pytest.raises(ValueError):
        await c.complete_sign_in(store, query, workspace_id='other', user_id='u')
    await c.complete_sign_in(store, query, workspace_id='w', user_id='u')
    assert seen[0]['client_id'] == 'issued-client'
    assert seen[0]['code_verifier'] and seen[0]['resource'] == c.RESOURCE
    assert c.oauth_record(store.effective)['subject'] == 'synthetic-account'
    with pytest.raises(ValueError):
        await c.complete_sign_in(store, query, workspace_id='w', user_id='u')


@pytest.mark.asyncio
async def test_disconnect_cancels_inflight_sign_in(monkeypatch):
    store = Store()
    workspace = str(store._db.workspace_id)
    query = parse_qs(urlsplit(await c.start_sign_in(store, workspace_id=workspace, user_id='u', callback='http://127.0.0.1:8080/auth/callback')).query)
    entered, proceed = asyncio.Event(), asyncio.Event()
    async def token(data):
        entered.set()
        await proceed.wait()
        return {'access_token':'access', 'refresh_token':'refresh', 'id_token':'id', 'scope':c.SCOPES}
    async def identity(*args, **kwargs):
        return {'sub':'account'}
    monkeypatch.setattr(c, '_token', token)
    monkeypatch.setattr(c, 'validate_identity', identity)
    callback = {'state':query['state'][0], 'code':'code', 'client_id':'issued'}
    task = asyncio.create_task(c.complete_sign_in(store, callback, workspace_id=workspace, user_id='u'))
    await entered.wait()
    with pytest.raises(ValueError):
        await c.complete_sign_in(store, callback, workspace_id=workspace, user_id='u')
    await c.disconnect(store)
    proceed.set()
    with pytest.raises(ValueError, match='cancelled'):
        await task
    assert not c.oauth_record(store.effective)


@pytest.mark.asyncio
async def test_catalog_excludes_cloud_and_preserves_selected_provider(monkeypatch):
    async def endpoint(request):
        return httpx.Response(200, json={'models':[{'name':'local:latest'}, {'name':'other:cloud'}, {'name':'alias','remote_model':'remote'}]})
    real = httpx.AsyncClient
    monkeypatch.setattr(c.httpx,'AsyncClient',lambda **kwargs:real(transport=httpx.MockTransport(endpoint),**kwargs))
    settings = Settings(studio_provider='ollama', ollama_model='other:cloud')
    assert await c.available_models(settings, 'ollama') == [{'id':'local:latest','name':'local:latest'}]
    with pytest.raises(StudioConfigurationError):
        await c.prepare_model(settings)
    settings.ollama_model = 'local:latest'
    await c.prepare_model(settings)
    assert settings.studio_provider == 'ollama'


@pytest.mark.integration
@pytest.mark.asyncio
async def test_encrypted_refresh_is_atomic_and_failure_keeps_connection(app, settings, monkeypatch):
    store = WorkspaceSettings(app.state.db, settings, build_cipher(settings.telegram_session_encryption_key))
    await store.load()
    await store.set_many({'studio.provider':'openai','studio.openai_model':'synthetic-model',
        'studio.openai_oauth':json.dumps({**record(), 'expires_at':0})})
    runtime = store.effective
    count = 0
    async def token(data):
        nonlocal count
        count += 1
        await asyncio.sleep(0.02)
        assert data['refresh_token'] == 'synthetic-refresh'
        return {'access_token':'rotated-access','refresh_token':'rotated-refresh','expires_in':3600}
    monkeypatch.setattr(c,'_token',token)
    from app.studio.model import run_settings
    frozen = run_settings(runtime)
    await asyncio.gather(c.refresh_openai(frozen), c.refresh_openai(runtime))
    assert c.oauth_record(frozen)["access_token"] == "rotated-access"
    assert count == 1
    assert c.oauth_record(runtime)['refresh_token'] == 'rotated-refresh'
    rows = await app.state.db._execute('SELECT value, is_secret FROM workspace_settings WHERE workspace_id=:workspace_id AND key=\'studio.openai_oauth\'',{})
    raw = rows.mappings().first()
    assert raw['is_secret'] and 'rotated-access' not in raw['value']
    assert 'rotated-access' not in json.dumps(store.as_dict(),default=str)
    await store.set('studio.openai_oauth',json.dumps({**c.oauth_record(runtime),'expires_at':0}))
    async def broken(data):
        raise ValueError('synthetic-provider-secret')
    monkeypatch.setattr(c,'_token',broken)
    with pytest.raises(StudioConfigurationError):
        await c.refresh_openai(runtime)
    assert c.oauth_record(runtime)['refresh_token'] == 'rotated-refresh'
    await c.disconnect(store)
    assert not c.oauth_record(runtime)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_settings_owner_csrf_catalog_and_callback(app, client, settings, monkeypatch):
    store = WorkspaceSettings(app.state.db, settings, build_cipher(settings.telegram_session_encryption_key))
    await store.load()
    app.state.workspace_settings = store
    app.state.settings = store.effective
    assert (await client.get('/settings/providers/models?provider=ollama',headers={'accept':'application/json'})).status_code == 401
    await client.post('/login',data={'username':settings.admin_username,'password':settings.admin_password})
    page = await client.get('/settings')
    assert 'Continue with ChatGPT' in page.text
    assert 'data-models-connected="false"' in page.text
    await store.set('studio.openai_oauth', json.dumps(record()))
    connected_page = await client.get('/settings')
    assert 'data-models-connected="true"' in connected_page.text
    assert 'synthetic-access' not in connected_page.text
    csrf = re.search('name="csrf_token" value="([^"]+)"',page.text).group(1)
    assert (await client.post('/settings/providers/openai/connect')).status_code == 403
    assert (await client.post('/settings/providers/openai/disconnect')).status_code == 403
    saved = await client.post('/settings/studio',data={'csrf_token':csrf,'provider':'ollama','ollama_model':'synthetic-local','ollama_url':'http://127.0.0.1:11434'},headers={'accept':'application/json'})
    assert saved.status_code == 200
    assert app.state.settings.studio_provider == 'ollama'
    async def catalog(runtime, provider, **kwargs):
        assert runtime.studio_provider == 'ollama'
        return [{'id':'synthetic-local','name':'synthetic-local'}]
    monkeypatch.setattr(c,'available_models',catalog)
    assert (await client.get('/settings/providers/models?provider=ollama')).json()['models'][0]['id'] == 'synthetic-local'
    captured=[]
    async def callback(store, query, **kwargs):
        captured.append(query)
    monkeypatch.setattr(c,'complete_sign_in',callback)
    response = await client.get('/auth/callback?state=synthetic-state&code=synthetic-code')
    assert response.status_code == 303 and captured[0]['code'] == 'synthetic-code'
    assert 'synthetic-code' not in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", ["completed", "failed", "missing"])
async def test_chatgpt_responses_contract_and_stream_completion(terminal):
    from pydantic_ai import UnexpectedModelBehavior
    seen = []
    async def endpoint(request):
        body = json.loads(request.content)
        seen.append(body)
        assert str(request.url) == c.RESOURCE + '/responses'
        assert request.headers['authorization'] == 'Bearer synthetic-access'
        assert body['stream'] is True and body['store'] is False
        response = {'id':'resp_synthetic', 'object':'response', 'model':'synthetic-model', 'created_at':1,
            'status':'in_progress', 'output':[], 'usage':None}
        message = {'id':'msg_synthetic','type':'message','role':'assistant','status':'in_progress','content':[]}
        events = [
            {'type':'response.created','response':response,'sequence_number':0},
            {'type':'response.output_item.added','output_index':0,'item':message,'sequence_number':1},
            {'type':'response.content_part.added','item_id':message['id'],'output_index':0,'content_index':0,
             'part':{'type':'output_text','text':'','annotations':[]},'sequence_number':2},
            {'type':'response.output_text.delta','item_id':message['id'],'output_index':0,'content_index':0,
             'delta':'Hello','sequence_number':3},
        ]
        if terminal != 'missing':
            events.append({'type':'response.'+terminal, 'sequence_number':4,
                'response':{**response,'status':terminal,'output':[{**message,'status':'completed',
                    'content':[{'type':'output_text','text':'Hello','annotations':[]}]}],
                    'usage':{'input_tokens':4,'output_tokens':1,'total_tokens':5,'input_tokens_details':{'cached_tokens':0},'output_tokens_details':{'reasoning_tokens':0}}}})
        return httpx.Response(200, headers={'content-type':'text/event-stream'},
            content=''.join('data: '+json.dumps(event)+'\n\n' for event in events)+'data: [DONE]\n\n')
    async with httpx.AsyncClient(transport=httpx.MockTransport(endpoint)) as client:
        model = ChatGPTPlanModel('synthetic-model',provider=OpenAIProvider(base_url=c.RESOURCE,api_key='synthetic-access',http_client=client))
        agent = Agent(model)
        if terminal == 'completed':
            result = await agent.run('Synthetic hello')
            assert result.output == 'Hello' and result.usage.input_tokens == 4
            async with agent.run_stream('Synthetic streamed hello') as result:
                assert await result.get_output() == 'Hello'
            assert len(seen) == 2
        else:
            with pytest.raises(UnexpectedModelBehavior):
                await agent.run('Synthetic hello')


@pytest.mark.asyncio
async def test_new_provider_settings_do_not_change_an_existing_run():
    from app.studio.model import run_settings
    settings = Settings(studio_provider='ollama', ollama_model='local-model')
    frozen = run_settings(settings)
    settings.studio_provider = 'openai'
    settings.openai_model = 'different-model'
    assert model_name(frozen) == 'local-model'
    assert frozen.studio_provider == 'ollama'
    assert model_name(run_settings(settings)) == 'different-model'


def test_external_consent_changes_with_chatgpt_account_and_local_needs_none():
    from app.studio.consent import configuration_fingerprint, consent_required
    a = Settings(studio_provider='openai',openai_model='model',studio_openai_oauth=json.dumps(record()))
    b = a.model_copy(update={'studio_openai_oauth':json.dumps({**record(),'subject':'different-account'})})
    assert configuration_fingerprint(a) != configuration_fingerprint(b)
    assert consent_required(a)
    assert not consent_required(Settings(studio_provider='ollama',ollama_model='local'))


@pytest.mark.integration
@pytest.mark.asyncio
async def test_connect_uses_fixed_loopback_and_member_cannot_manage_accounts(app, settings):
    from dataclasses import replace
    store = WorkspaceSettings(app.state.db, settings, build_cipher(settings.telegram_session_encryption_key))
    await store.load()
    app.state.workspace_settings = store
    app.state.settings = store.effective
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1:8080') as client:
        await client.post('/login',data={'username':settings.admin_username,'password':settings.admin_password})
        page = await client.get('/settings')
        csrf = re.search('name="csrf_token" value="([^"]+)"',page.text).group(1)
        response = await client.post('/settings/providers/openai/connect',data={'csrf_token':csrf})
        assert response.status_code == 303
        url = urlsplit(response.headers['location'])
        assert url.scheme == 'https' and url.hostname == 'auth.openai.com'
        params = parse_qs(url.query)
        assert params['redirect_uri'] == ['http://127.0.0.1:8080/auth/callback']
        assert params['client_id'] == ['dynamic_agent_client']
        app.state.workspace_context = replace(app.state.workspace_context, role='member')
        assert (await client.get('/settings/providers/models?provider=ollama')).status_code == 403
        assert (await client.post('/settings/providers/openai/disconnect',data={'csrf_token':csrf})).status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", ["account", "api"])
async def test_openai_catalog_keeps_all_available_models_and_provider(monkeypatch, shape):
    settings = Settings(studio_provider="ollama", studio_openai_oauth=json.dumps(record()))
    entries = [{"slug" if shape == "account" else "id": f"synthetic-{i}",
                "display_name": f"Synthetic {i}"} for i in range(251)]
    entries += [entries[0], None, "invalid", {}, {"id": "bad id"},
                {"slug": "hidden-model", "visibility": "hide"},
                {"slug": "bad-name", "display_name": None}]
    async def endpoint(request):
        assert str(request.url) == c.RESOURCE + "/models"
        assert request.headers["authorization"] == "Bearer synthetic-access"
        return httpx.Response(200, json={"models" if shape == "account" else "data": entries})
    client_class = httpx.AsyncClient
    monkeypatch.setattr(c.httpx, "AsyncClient", lambda **kwargs: client_class(transport=httpx.MockTransport(endpoint), **kwargs))
    models = await c.available_models(settings, "openai")
    assert len(models) == 252
    assert models[250] == {"id": "synthetic-250", "name": "Synthetic 250"}
    assert models[-1] == {"id": "bad-name", "name": "bad-name"}
    assert settings.studio_provider == "ollama"


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [{}, {"models": {}}, [], {"data": None}])
async def test_openai_catalog_rejects_invalid_envelopes(monkeypatch, payload):
    settings = Settings(studio_provider="openai", studio_openai_oauth=json.dumps(record()))
    client_class = httpx.AsyncClient
    monkeypatch.setattr(c.httpx, "AsyncClient", lambda **kwargs: client_class(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload)), **kwargs))
    with pytest.raises(ValueError, match="Invalid model catalog"):
        await c.available_models(settings, "openai")
