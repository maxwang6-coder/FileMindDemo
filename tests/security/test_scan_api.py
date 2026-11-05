import json
import sqlite3
import threading
import time
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest

from app import main, scan
from app.config import Settings
from app.db import DB_NAME, initialize_database, read_file_records
from app.jobs import create_scan_job, start_scan_job, read_job


# 建立临时服务并捕获测试会话凭证，为鉴权或扫描接口测试准备客户端。
@pytest.fixture
def session(tmp_path, monkeypatch):
    root = tmp_path / 'documents'
    root.mkdir()
    (root / 'notes.txt').write_bytes(b'lesson')
    settings = Settings(DATA_DIR=tmp_path / 'data', ROOT_PATH=root)
    initialize_database(settings, root_id='demo')
    tokens = []
    monkeypatch.setattr(main, 'print_session_token', lambda token, origin: tokens.append(token))
    with TestClient(main.create_app(settings), base_url=main.ORIGIN) as client:
        yield client, {'Authorization': 'Bearer ' + tokens[0]}, settings


# 在有限时间内查询测试任务，直到结束或明确报告超时失败。
def wait_for_job(client, headers, job_id):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        response = client.get('/api/jobs/' + job_id, headers=headers)
        assert response.status_code == 200
        job = response.json()
        if job['state'] in ('succeeded', 'failed', 'interrupted'):
            return job
        time.sleep(0.01)
    pytest.fail('Scan did not finish')


# 验证扫描 API 完成并保存报告，不泄露绝对路径或凭证，且幂等重试不新建任务。
def test_scan_api_runs_and_persists_report_without_paths_or_tokens(session):
    client, headers, settings = session
    status = client.get('/api/status', headers=headers).json()
    assert status['roots'] == [{'id': 'demo', 'alias': 'demo'}]
    assert str(settings.ROOT_PATH) not in json.dumps(status)
    response = client.post('/api/scans', headers={**headers, 'Idempotency-Key': 'scan-once'}, json={'root_id': 'demo', 'verify_content': True})
    assert response.status_code == 202
    job = wait_for_job(client, headers, response.json()['job_id'])
    assert job['state'] == 'succeeded' and job['result']['complete']
    assert job['result']['counts']['scanned'] == job['result']['counts']['added'] == 1
    assert len(read_file_records(settings.DATA_DIR, 'demo')) == 1
    assert str(settings.ROOT_PATH) not in json.dumps(job)
    assert headers['Authorization'] not in json.dumps(job)
    assert client.get('/api/scans/latest/demo', headers=headers).json()['job']['id'] == job['id']
    repeated = client.post('/api/scans', headers={**headers, 'Idempotency-Key': 'scan-once'}, json={'root_id': 'demo', 'verify_content': True})
    assert repeated.json() == response.json()
    conflict = client.post('/api/scans', headers={**headers, 'Idempotency-Key': 'scan-once'}, json={'root_id': 'demo', 'verify_content': False})
    assert conflict.status_code == 409
    with sqlite3.connect(settings.DATA_DIR / DB_NAME) as db:
        assert db.execute('SELECT count(*) FROM jobs').fetchone()[0] == 1


# 验证扫描鉴权、来源、参数、请求编号与未知 ID 校验，GET 不启动扫描。
def test_scan_api_auth_origin_validation_and_no_get_actions(session):
    client, headers, settings = session
    path = '/api/scans'
    payload = {'root_id': 'demo'}
    assert client.post(path, json=payload).status_code == 401
    assert client.post(path, headers={**headers, 'Origin': 'https://evil.example', 'Idempotency-Key': 'x'}, json=payload).status_code == 403
    assert client.get(path, headers=headers).status_code == 405
    assert client.post(path, headers=headers, json=payload).status_code == 422
    for extra in ({'path': '/etc'}, {'ROOT_PATH': '/etc'}, {'verify_content': 'yes'}):
        assert client.post(path, headers={**headers, 'Idempotency-Key': 'x'}, json={**payload, **extra}).status_code == 422
    assert client.post(path, headers={**headers, 'Idempotency-Key': 'x'}, json={'root_id': 'unknown'}).status_code == 404
    assert client.get('/api/jobs/' + str(uuid4()), headers=headers).status_code == 404
    assert client.get('/api/scans/latest/demo').status_code == 401
    assert read_file_records(settings.DATA_DIR, 'demo') == []


# 验证后台任务运行时仍可查询健康状态，并拒绝新任务而复用相同申请。
def test_worker_is_bounded_and_health_is_responsive(session, monkeypatch):
    client, headers, settings = session
    entered, release = threading.Event(), threading.Event()
    original = scan.scan_root
    # 暂停后台扫描直到测试放行，用于检查容量限制和健康请求响应。
    def blocked(*args, **kwargs):
        entered.set()
        assert release.wait(timeout=5)
        return original(*args, **kwargs)
    monkeypatch.setattr(scan, 'scan_root', blocked)
    try:
        response = client.post('/api/scans', headers={**headers, 'Idempotency-Key': 'one'}, json={'root_id': 'demo'})
        assert response.status_code == 202 and entered.wait(timeout=2)
        assert client.get('/health').status_code == 200
        assert client.post('/api/scans', headers={**headers, 'Idempotency-Key': 'two'}, json={'root_id': 'demo'}).status_code == 429
        assert client.post('/api/scans', headers={**headers, 'Idempotency-Key': 'one'}, json={'root_id': 'demo'}).json() == response.json()
    finally:
        release.set()
    assert wait_for_job(client, headers, response.json()['job_id'])['state'] == 'succeeded'


# 验证重启把遗留任务标为中断，不自动重新扫描或生成文件记录。
def test_restart_marks_interrupted_jobs_without_replaying(tmp_path, monkeypatch):
    root = tmp_path / 'docs'
    root.mkdir()
    (root / 'notes.txt').write_bytes(b'a')
    config = Settings(DATA_DIR=tmp_path / 'data', ROOT_PATH=root)
    initialize_database(config, root_id='demo')
    job_id = create_scan_job(config.DATA_DIR, 'demo')
    start_scan_job(config.DATA_DIR, job_id)
    monkeypatch.setattr(main, 'print_session_token', lambda *args: None)
    with TestClient(main.create_app(config), base_url=main.ORIGIN):
        assert read_job(config.DATA_DIR, job_id)['state'] == 'interrupted'
        assert read_file_records(config.DATA_DIR, 'demo') == []
