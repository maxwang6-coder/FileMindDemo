from dataclasses import replace
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from app.config import Settings
from app.db import DB_NAME, initialize_database
from app.scan import collect_file_metadata


# 在临时目录创建授权资料根与独立数据库，供文件测试使用。
@pytest.fixture
def authorized(tmp_path):
    root = tmp_path / 'documents'
    root.mkdir()
    settings = Settings(DATA_DIR=tmp_path / 'data', ROOT_PATH=root)
    initialize_database(settings, root_id='demo')
    return settings, root


# 读取测试根表与文件表的快照，检查只读操作没有写入。
def database_rows(settings):
    with sqlite3.connect(settings.DATA_DIR / DB_NAME) as db:
        return (db.execute('SELECT * FROM roots').fetchall(),
                db.execute('SELECT * FROM files').fetchall())


# 验证元数据清点保留相对路径、大小、纳秒时间、设备号与 inode。
def test_metadata_uses_relative_paths_and_full_stat_values(authorized):
    settings, root = authorized
    (root / '课程').mkdir()
    path = root / '课程' / '讲义 (一).TXT'
    path.write_text('中文内容\n', encoding='utf-8')
    info = path.lstat()
    result = collect_file_metadata(settings, 'demo')
    assert result.complete is True
    assert len(result.files) == 1
    record = result.files[0]
    assert record.relpath == '课程/讲义 (一).TXT'
    assert (record.size, record.mtime_ns, record.ctime_ns, record.st_dev, record.st_ino) == (
        len('中文内容\n'.encode()), info.st_mtime_ns, info.st_ctime_ns, info.st_dev, info.st_ino)


# 验证不同目录的同名同内容文件分别列为独立候选。
def test_same_basename_and_copied_bytes_are_separate_candidates(authorized):
    settings, root = authorized
    for name in ('a', 'b'):
        (root / name).mkdir()
        (root / name / 'notes.txt').write_bytes(b'same content')
    result = collect_file_metadata(settings, 'demo')
    assert {r.relpath for r in result.files} == {'a/notes.txt', 'b/notes.txt'}
    assert len(result.files) == 2


# 验证类型白名单和排除规则正确跳过隐藏项、链接与非普通文件。
def test_whitelist_hidden_directories_links_and_nonregular_files(authorized, tmp_path):
    settings, root = authorized
    for name in ('a.txt', 'b.md', 'c.PDF', 'd.docx'):
        (root / name).write_bytes(b'Not parsed in step 2')
    for name in ('picture.png', 'fixture_manifest.json', 'program.py', 'no_extension', '.secret.txt'):
        (root / name).write_bytes(b'exclude')
    for name in ('.git', 'node_modules', 'Example.app', 'Dropbox'):
        (root / name).mkdir()
        (root / name / 'must-not-appear.txt').write_bytes(b'exclude')
    outside = tmp_path / 'outside.txt'
    outside.write_bytes(b'outside')
    (root / 'escape.txt').symlink_to(outside)
    (root / 'inside_link.txt').symlink_to(root / 'a.txt')
    (root / 'dangling.txt').symlink_to(tmp_path / 'absent')
    os.mkfifo(root / 'pipe.txt')
    result = collect_file_metadata(settings, 'demo')
    assert {r.relpath for r in result.files} == {'a.txt', 'b.md', 'c.PDF', 'd.docx'}
    assert result.skipped['symbolic_link'] == 3
    assert result.skipped['not_regular_file'] == 1
    assert result.skipped['extension_not_allowed'] == 4


# 验证只清点元数据时不打开源内容、不改变源属性或数据库记录。
def test_no_file_body_reads_or_database_writes(authorized, monkeypatch):
    settings, root = authorized
    path = root / 'notes.txt'
    path.write_text('must remain unchanged')
    before_stat = path.lstat()
    before_db = database_rows(settings)
    original_open = Path.open
    # 禁止打开授权资料内容，其他文件打开仍交给原实现。
    def reject_body_read(self, *args, **kwargs):
        if self.is_relative_to(root):
            raise AssertionError('Metadata collection must not open source contents')
        return original_open(self, *args, **kwargs)
    monkeypatch.setattr(Path, 'open', reject_body_read)
    first = collect_file_metadata(settings, 'demo')
    second = collect_file_metadata(settings, 'demo')
    assert first.files == second.files
    after_stat = path.lstat()
    assert (after_stat.st_size, after_stat.st_mtime_ns, after_stat.st_ctime_ns) == (
        before_stat.st_size, before_stat.st_mtime_ns, before_stat.st_ctime_ns)
    assert database_rows(settings) == before_db


# 验证旧根登记不能绕过当前配置授权、启用状态或设备号校验。
@pytest.mark.parametrize('kind', ['no_grant', 'different_root', 'unknown_id', 'disabled', 'changed_device'])
def test_database_record_does_not_expand_current_authorization(authorized, tmp_path, kind):
    settings, root = authorized
    (root / 'notes.txt').write_bytes(b'private source')
    root_id = 'demo'
    if kind == 'no_grant':
        settings = replace(settings, ROOT_PATH=None)
    elif kind == 'different_root':
        other = tmp_path / 'other'
        other.mkdir()
        settings = replace(settings, ROOT_PATH=other)
    elif kind == 'unknown_id':
        root_id = 'unknown'
    else:
        with sqlite3.connect(settings.DATA_DIR / DB_NAME) as db:
            if kind == 'disabled':
                db.execute('UPDATE roots SET enabled=0')
            else:
                db.execute('UPDATE roots SET dev=dev+1')
    with pytest.raises((ValueError, PermissionError)):
        collect_file_metadata(settings, root_id)


# 验证候选超限明确失败，不把截断名单冒充成功结果。
def test_file_limit_never_returns_silently_truncated_success(authorized):
    settings, root = authorized
    (root / 'a.txt').write_bytes(b'a')
    (root / 'b.txt').write_bytes(b'b')
    assert len(collect_file_metadata(replace(settings, MAX_FILES=2), 'demo').files) == 2
    with pytest.raises(ValueError, match='MAX_FILES'):
        collect_file_metadata(replace(settings, MAX_FILES=1), 'demo')
    assert database_rows(settings)[1] == []


# 验证元数据读取期间文件消失时中止检查，数据库保持原样。
def test_disappearing_file_aborts_without_changing_database(authorized, monkeypatch):
    settings, root = authorized
    path = root / 'notes.txt'
    path.write_bytes(b'notes')
    original = Path.lstat
    # 对目标文件模拟属性读取时消失，其余路径保留真实行为。
    def disappear(self, *args, **kwargs):
        if self == path:
            raise FileNotFoundError('simulated concurrent removal')
        return original(self, *args, **kwargs)
    monkeypatch.setattr(Path, 'lstat', disappear)
    with pytest.raises(FileNotFoundError):
        collect_file_metadata(settings, 'demo')
    assert database_rows(settings)[1] == []


# 验证元数据 CLI 输出相对路径 JSON，并拒绝缺少本机根授权的请求。
def test_metadata_cli_returns_json_and_requires_explicit_authorization(authorized, tmp_path):
    settings, root = authorized
    (root / 'notes.txt').write_bytes(b'notes')
    config = tmp_path / 'config.local.toml'
    config.write_text('DATA_DIR = ' + json.dumps(str(settings.DATA_DIR)) + '\nROOT_PATH = '
                      + json.dumps(str(root)) + '\n')
    args = [sys.executable, '-m', 'scripts.scan', '--config', str(config), '--root-id', 'demo', '--metadata-only']
    run = subprocess.run(args, capture_output=True, text=True, timeout=10)
    assert run.returncode == 0, run.stderr
    output = json.loads(run.stdout)
    assert output['mode'] == 'metadata_only' and output['complete'] is True
    assert output['files'][0]['relpath'] == 'notes.txt'
    assert str(root) not in run.stdout
    assert 'file_id' not in output['files'][0] and 'sha256' not in output['files'][0]
    config.write_text('DATA_DIR = ' + json.dumps(str(settings.DATA_DIR)) + '\n')
    run = subprocess.run(args, capture_output=True, text=True, timeout=10)
    assert run.returncode != 0
    assert json.loads(run.stdout)['complete'] is False
    assert 'Traceback' not in run.stderr
