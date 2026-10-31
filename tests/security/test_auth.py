import pytest
import re
from fastapi.testclient import TestClient

from app import main
from app import auth
from app.config import Settings


@pytest.fixture
def session(monkeypatch, tmp_path):
    printed = []
    monkeypatch.setattr(main, 'print_session_token', lambda token, origin: printed.append(token))
    with TestClient(main.create_app(Settings(DATA_DIR=tmp_path)), base_url=main.ORIGIN) as client:
        yield client, printed[0]


def test_api_requires_bearer_on_all_routes_and_methods(session):
    client, token = session
    for path in ('/api', '/api/status', '/api/missing', '/api/status/'):
        for method in ('GET', 'POST', 'OPTIONS', 'HEAD'):
            assert client.request(method, path).status_code == 401
    assert client.get('/api/status', headers={'Authorization': 'Bearer wrong'}).status_code == 401
    assert client.get('/api/status?token=' + token).status_code == 401
    assert client.get('/api/status', headers={'Authorization': 'Bearer ' + token}).status_code == 200


@pytest.mark.parametrize('origin', ['https://evil.example', 'null', 'http://127.0.0.1:8000', 'https://127.0.0.1:8765'])
def test_foreign_origin_rejected_even_with_token(session, origin):
    client, token = session
    response = client.get('/api/status', headers={'Authorization': 'Bearer ' + token, 'Origin': origin})
    assert response.status_code == 403
    assert 'access-control-allow-origin' not in response.headers


@pytest.mark.parametrize('host', ['evil.example:8765', '127.0.0.1.evil.example:8765', 'localhost:8765', '127.0.0.1:8000'])
def test_invalid_host_rejected(session, host):
    client, token = session
    assert client.get('/api/status', headers={'Host': host, 'Authorization': 'Bearer ' + token}).status_code == 400


@pytest.mark.parametrize('origin', [None, '', main.ORIGIN])
def test_expected_origin_and_missing_origin_allowed(session, origin):
    client, token = session
    headers = {'Authorization': 'Bearer ' + token}
    if origin is not None:
        headers['Origin'] = origin
    assert client.get('/api/status', headers=headers).status_code == 200


def test_duplicate_security_headers_rejected(session):
    client, token = session
    for name, value, expected in [('Host', f'{main.HOST}:{main.PORT}', 400), ('Origin', main.ORIGIN, 403), ('Authorization', 'Bearer ' + token, 401)]:
        response = client.get('/api/status', headers=[(name, value), (name, value)])
        assert response.status_code == expected


def test_public_assets_contain_no_token_and_are_not_cached(session):
    client, token = session
    script_path = re.search(r'<script[^>]+src="([^"]+)"', client.get('/').text).group(1)
    assert script_path.startswith('/static/assets/')
    for path in ('/', script_path):
        response = client.get(path)
        assert response.status_code == 200
        assert token not in response.text
        assert response.headers['cache-control'] == 'no-store'
        assert 'access-control-allow-origin' not in response.headers
    script = (main.STATIC_DIR / 'src' / 'App.vue').read_text()
    assert 'localStorage' not in script and 'sessionStorage' not in script
    assert client.get('/static/src/App.vue').status_code == 404


def test_restart_rotates_token(monkeypatch, tmp_path):
    tokens = []
    monkeypatch.setattr(main, 'print_session_token', lambda token, origin: tokens.append(token))
    app = main.create_app(Settings(DATA_DIR=tmp_path))
    with TestClient(app, base_url=main.ORIGIN):
        pass
    with TestClient(app, base_url=main.ORIGIN) as client:
        assert tokens[0] != tokens[1]
        assert client.get('/api/status', headers={'Authorization': 'Bearer ' + tokens[0]}).status_code == 401


def test_launcher_binds_only_loopback(monkeypatch):
    captured = {}
    monkeypatch.setattr(main.sys, 'argv', ['app.main'])
    monkeypatch.setattr(main.uvicorn, 'run', lambda app, **kwargs: captured.update(kwargs))
    main.main()
    assert captured['host'] == '127.0.0.1'
    assert captured['port'] == 8765
    assert captured['proxy_headers'] is False
    assert captured['access_log'] is False


def test_manual_uvicorn_entry(monkeypatch, tmp_path):
    from uvicorn.importer import import_from_string

    # Load the exact object named by the manual, with isolated runtime data.
    application = import_from_string('app.main:app')
    assert application is main.app
    assert main.ORIGIN == 'http://127.0.0.1:8765'
    # The application captures configuration in its lifespan; redirect its
    # lock boundary to a temporary directory without touching the real one.
    original_lock = main.data_directory_lock
    monkeypatch.setattr(main, 'data_directory_lock', lambda _: original_lock(tmp_path))
    tokens = []
    monkeypatch.setattr(main, 'print_session_token', lambda token, origin: tokens.append(token))
    with TestClient(application, base_url=main.ORIGIN) as client:
        assert client.get('/health').json() == {'status': 'ok', 'version': main.VERSION}
        assert client.get('/api/status').status_code == 401
        headers = {'Authorization': 'Bearer ' + tokens[0], 'Origin': main.ORIGIN}
        assert client.get('/api/status', headers=headers).status_code == 200
        assert client.get('/health', headers={'Host': '127.0.0.1:8000'}).status_code == 400


def test_token_output_uses_only_terminal(monkeypatch, capsys):
    from io import StringIO

    class Terminal(StringIO):
        def close(self):
            pass

    terminal = Terminal()
    opened = []

    def open_terminal(path, mode, **kwargs):
        opened.append((path, mode))
        return terminal

    monkeypatch.setattr('builtins.open', open_terminal)
    auth.print_session_token('test-secret', main.ORIGIN)
    assert opened == [('/dev/tty', 'w')]
    assert 'test-secret' in terminal.getvalue()
    output = capsys.readouterr()
    assert not output.out and not output.err


def test_missing_terminal_stops_startup(monkeypatch, tmp_path):
    def unavailable(*args, **kwargs):
        raise OSError('no controlling terminal')

    monkeypatch.setattr('builtins.open', unavailable)
    with pytest.raises(RuntimeError, match='local interactive terminal'):
        with TestClient(main.create_app(Settings(DATA_DIR=tmp_path)), base_url=main.ORIGIN):
            pass
