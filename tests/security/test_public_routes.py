import pytest
import re
from fastapi.testclient import TestClient

from app import main
from app.config import Settings


def frontend_paths(client):
    script = re.search(r'<script[^>]+src="([^"]+)"', client.get('/').text).group(1)
    return ('/', '/health', script)


@pytest.fixture
def make_client(monkeypatch, tmp_path):
    monkeypatch.setattr(main, 'print_session_token', lambda token, origin: None)
    return lambda settings=None: TestClient(
        main.create_app(settings or Settings(DATA_DIR=tmp_path / 'data')), base_url=main.ORIGIN)


def test_health_returns_only_status_and_version(make_client, tmp_path):
    with make_client(Settings(ROOT_PATH=tmp_path, DATA_DIR=tmp_path / 'private')) as client:
        response = client.get('/health')
        assert response.status_code == 200
        assert response.json() == {'status': 'ok', 'version': main.VERSION}
        assert str(tmp_path) not in response.text
        assert response.headers['cache-control'] == 'no-store'
        assert client.get('/health', headers={'Host': 'evil.example'}).status_code == 400
        assert client.get('/health', headers={'Origin': 'https://evil.example'}).status_code == 403


@pytest.mark.parametrize('write,provider,labels', [
    (False, 'mock', ('只读', 'Mock（模拟）')),
    (True, 'mock', ('允许写入', 'Mock（模拟）')),
    (False, 'remote', ('只读', '真实模型（尚未接入）')),
])
def test_home_displays_modes_before_login(make_client, tmp_path, write, provider, labels):
    with make_client(Settings(DATA_DIR=tmp_path / 'data', WRITE_ENABLED=write, MODEL_PROVIDER=provider,
                              CLOUD_ENABLED=provider != 'mock')) as client:
        response = client.get('/')
        assert response.status_code == 200
        assert all(label in response.text for label in labels)
        assert '{{WRITE_MODE}}' not in response.text
        assert '{{MODEL_MODE}}' not in response.text


def test_public_pages_do_not_expose_secrets_or_paths(make_client, monkeypatch, tmp_path):
    secret = 'secret-marker-not-for-browser'
    monkeypatch.setenv('OPENAI_API_KEY', secret)
    configuration = Settings(ROOT_PATH=tmp_path, DATA_DIR=tmp_path / 'private',
                             CLOUD_ENABLED=True, MODEL_PROVIDER=secret)
    with make_client(configuration) as client:
        for path in frontend_paths(client):
            response = client.get(path)
            assert secret not in response.text
            assert str(tmp_path) not in response.text


def test_get_requests_cannot_mutate_or_open_files(make_client, monkeypatch, tmp_path):
    tokens = []
    monkeypatch.setattr(main, 'print_session_token', lambda token, origin: tokens.append(token))
    root = tmp_path / 'documents'
    root.mkdir()
    document = root / 'example.txt'
    document.write_text('unchanged')

    # Opening files in an external application must never happen during GET.
    def forbid_external_open(*args, **kwargs):
        raise AssertionError('GET launched an external process')

    monkeypatch.setattr('subprocess.Popen', forbid_external_open)
    with make_client(Settings(ROOT_PATH=root, DATA_DIR=tmp_path / 'data')) as client:
        data_before = {p.name: p.read_bytes() for p in (tmp_path / 'data').iterdir()}
        headers = {'Authorization': 'Bearer ' + tokens[0]}
        for path in (*frontend_paths(client), '/api/status'):
            assert client.get(path, headers=headers, params={
                'path': str(document), 'action': 'delete', 'open': 'true',
            }).status_code == 200
        for path in ('/api/open', '/api/delete', '/api/move', '/api/write'):
            assert client.get(path, headers=headers).status_code == 404
        assert client.post('/health', headers=headers).status_code == 405
        assert client.post('/', headers=headers).status_code == 405
    assert document.read_text() == 'unchanged'
    assert list(root.iterdir()) == [document]
    assert {p.name: p.read_bytes() for p in (tmp_path / 'data').iterdir()} == data_before
