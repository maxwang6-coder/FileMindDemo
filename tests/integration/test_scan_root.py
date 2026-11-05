from contextlib import contextmanager
from dataclasses import replace
import hashlib
import json
import sqlite3

import pytest

from app import scan
from app.config import Settings
from app.db import DB_NAME, initialize_database, read_file_records, check_existing_database
from app.jobs import read_job


# 在临时目录创建授权资料根与独立数据库，供文件测试使用。
@pytest.fixture
def authorized(tmp_path):
    root = tmp_path / 'docs'
    root.mkdir()
    config = Settings(DATA_DIR=tmp_path / 'data', ROOT_PATH=root)
    initialize_database(config, root_id='demo')
    return config, root


# 临时替换目录打开函数，为指定扫描范围注入权限失败。
def deny_directory(monkeypatch, relative):
    original = scan._open_scan_directory
    # 对指定相对目录模拟无法访问，其他目录仍使用真实安全打开流程。
    @contextmanager
    def denied(root, relpath):
        if relpath == relative:
            raise PermissionError('injected unavailable directory')
        with original(root, relpath) as fd:
            yield fd
    monkeypatch.setattr(scan, '_open_scan_directory', denied)


# 验证失败子树旧记录暂不可用，正常范围继续更新，恢复后保留原编号。
def test_unavailable_subtree_is_hidden_and_other_scopes_continue(authorized, monkeypatch):
    config, root = authorized
    (root / 'blocked').mkdir()
    (root / 'blocked' / 'nested').mkdir()
    (root / 'blocked' / 'nested' / 'keep.txt').write_bytes(b'keep')
    (root / 'gone.txt').write_bytes(b'gone')
    scan.scan_root(config, 'demo')
    before = {r['relpath']: r for r in read_file_records(config.DATA_DIR, 'demo')}
    (root / 'gone.txt').unlink()
    (root / 'new.txt').write_bytes(b'new')
    deny_directory(monkeypatch, 'blocked')
    report = scan.scan_root(config, 'demo')
    after = {r['relpath']: r for r in read_file_records(config.DATA_DIR, 'demo')}
    assert report['applied'] and not report['complete']
    assert after['blocked/nested/keep.txt']['status'] == 'unavailable'
    assert after['blocked/nested/keep.txt']['id'] == before['blocked/nested/keep.txt']['id']
    assert after['gone.txt']['status'] == 'missing'
    assert after['new.txt']['status'] == 'discovered'
    assert report['counts']['unavailable'] == report['counts']['missing'] == 1
    job = read_job(config.DATA_DIR, report['scan_id'])
    assert job['result'] == report and job['error_code'] == 'partial_scan'
    assert job['started_at'] and job['finished_at']
    monkeypatch.undo()
    recovered = scan.scan_root(config, 'demo')
    current = {r['relpath']: r for r in read_file_records(config.DATA_DIR, 'demo')}
    assert recovered['complete']
    assert current['blocked/nested/keep.txt']['status'] == 'discovered'
    assert current['blocked/nested/keep.txt']['id'] == before['blocked/nested/keep.txt']['id']


# 验证根目录无法列举时旧记录标不可用，而不是批量标缺失。
def test_unavailable_root_does_not_mass_delete(authorized, monkeypatch):
    config, root = authorized
    (root / 'keep.txt').write_bytes(b'a')
    scan.scan_root(config, 'demo')
    deny_directory(monkeypatch, '')
    report = scan.scan_root(config, 'demo')
    assert not report['complete'] and report['counts']['missing'] == 0
    assert read_file_records(config.DATA_DIR, 'demo')[0]['status'] == 'unavailable'


# 验证成功枚举父目录确认子目录消失后，其旧后代记录可标缺失。
def test_missing_parent_in_complete_scope_marks_descendants_missing(authorized):
    config, root = authorized
    (root / 'sub').mkdir()
    path = root / 'sub' / 'gone.txt'
    path.write_bytes(b'a')
    scan.scan_root(config, 'demo')
    path.unlink()
    (root / 'sub').rmdir()
    assert scan.scan_root(config, 'demo')['counts']['missing'] == 1


# 验证单文件不稳定不阻塞正常文件，恢复后仍能正确比较内容版本。
def test_failed_file_does_not_block_healthy_file_or_reuse_old_evidence(authorized, monkeypatch):
    config, root = authorized
    (root / 'bad.txt').write_bytes(b'old')
    scan.scan_root(config, 'demo')
    before = read_file_records(config.DATA_DIR, 'demo')[0]
    (root / 'bad.txt').write_bytes(b'new')
    (root / 'good.txt').write_bytes(b'good')
    original = scan._hash_file
    # 只把指定文件的观察改为不稳定，模拟单文件故障并保留正常结果。
    def unstable(root, candidate, **kwargs):
        result = original(root, candidate, **kwargs)
        return replace(result, sha256=None, status='unstable', error_code='changed_during_read') if candidate.relpath == 'bad.txt' else result
    monkeypatch.setattr(scan, '_hash_file', unstable)
    report = scan.scan_root(config, 'demo')
    assert report['counts']['stable'] == report['counts']['failed'] == 1
    assert report['failed_files'][0]['sha256'] is None
    assert read_file_records(config.DATA_DIR, 'demo')[0]['status'] in ('unstable', 'discovered')
    rows = {r['relpath']: r for r in read_file_records(config.DATA_DIR, 'demo')}
    assert rows['bad.txt']['status'] == 'unstable'
    assert rows['good.txt']['status'] == 'discovered'
    monkeypatch.undo()
    scan.scan_root(config, 'demo')
    rows = {r['relpath']: r for r in read_file_records(config.DATA_DIR, 'demo')}
    assert rows['bad.txt']['id'] == before['id']
    assert rows['bad.txt']['content_rev'] == before['content_rev'] + 1


# 验证正式扫描跳过边界链接和排除项，且报告不含根外哨兵内容。
def test_links_exclusions_and_report_do_not_read_boundary(authorized, tmp_path, monkeypatch):
    config, root = authorized
    outside = tmp_path / 'sentinel.txt'
    outside.write_bytes(b'private sentinel')
    (root / 'escape.txt').symlink_to(outside)
    (root / '.hidden').mkdir()
    (root / '.hidden' / 'secret.txt').write_bytes(b'not scanned')
    (root / 'program.py').write_bytes(b'not a document')
    (root / 'safe.txt').write_bytes(b'safe')
    original = scan._read_sha256
    calls = []
    # 记录哈希读取调用，再执行真实读取，用于检查没有越界读取。
    def spy(stream, **kwargs):
        calls.append(stream.name)
        return original(stream, **kwargs)
    monkeypatch.setattr(scan, '_read_sha256', spy)
    report = scan.scan_root(config, 'demo')
    assert len(calls) == 1
    assert report['skipped'] == {'excluded_name': 1, 'symbolic_link': 1, 'extension_not_allowed': 1}
    assert 'private sentinel' not in json.dumps(report)
    assert len(read_file_records(config.DATA_DIR, 'demo')) == 1


# 验证总文件数超限时不应用任何文件更新，但保留失败任务。
def test_limits_abort_all_file_changes_but_save_failed_job(authorized):
    config, root = authorized
    for name in ('a.txt', 'b.txt'):
        (root / name).write_bytes(b'a')
    with pytest.raises(ValueError, match='MAX_FILES'):
        scan.scan_root(replace(config, MAX_FILES=1), 'demo')
    assert read_file_records(config.DATA_DIR, 'demo') == []
    with sqlite3.connect(config.DATA_DIR / DB_NAME) as db:
        assert db.execute('SELECT state,error_code FROM jobs').fetchall() == [('failed', 'max_files_exceeded')]


# 验证数据库从版本 1 迁移到 2 后原文件编号和记录保持不变。
def test_v1_database_migrates_without_losing_identity(authorized):
    config, root = authorized
    (root / 'keep.txt').write_bytes(b'a')
    scan.scan_root(config, 'demo')
    before = read_file_records(config.DATA_DIR, 'demo')
    with sqlite3.connect(config.DATA_DIR / DB_NAME) as db:
        db.execute('DROP TABLE jobs')
        db.execute('DROP TABLE idempotency')
        db.execute('PRAGMA user_version=1')
    check_existing_database(config.DATA_DIR)
    assert read_file_records(config.DATA_DIR, 'demo') == before
    with sqlite3.connect(config.DATA_DIR / DB_NAME) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 2


# 验证正式扫描入口的重复稳定、复制独立编号、改名保号与删除标记。
def test_full_scan_identity_rules_repeat_copy_rename_and_delete(authorized):
    config, root = authorized
    original = root / 'original.txt'
    original.write_bytes(b'lesson')
    scan.scan_root(config, 'demo')
    first = read_file_records(config.DATA_DIR, 'demo')[0]
    assert scan.scan_root(config, 'demo')['counts']['unchanged'] == 1
    (root / 'copy.txt').write_bytes(original.read_bytes())
    scan.scan_root(config, 'demo')
    rows = {r['relpath']: r for r in read_file_records(config.DATA_DIR, 'demo')}
    assert rows['copy.txt']['id'] != first['id']
    assert rows['copy.txt']['sha256'] == first['sha256']
    original.rename(root / 'renamed.txt')
    assert scan.scan_root(config, 'demo')['counts']['renamed'] == 1
    rows = {r['relpath']: r for r in read_file_records(config.DATA_DIR, 'demo')}
    assert rows['renamed.txt']['id'] == first['id']
    (root / 'renamed.txt').unlink()
    report = scan.scan_root(config, 'demo')
    assert report['counts']['missing'] == 1
    assert (root / 'copy.txt').read_bytes() == b'lesson'


# 验证目录枚举中途失败时丢弃缺失证据，不把未列完的文件当删除。
def test_partial_directory_iterator_never_proves_absence(authorized, monkeypatch):
    config, root = authorized
    (root / 'sub').mkdir()
    for name in ('a.txt', 'b.txt'):
        (root / 'sub' / name).write_bytes(b'a')
    scan.scan_root(config, 'demo')
    original = scan.os.scandir
    inode = (root / 'sub').stat().st_ino
    # 模拟指定子目录只列出部分条目后失败，其他目录正常枚举。
    @contextmanager
    def interrupted(fd):
        with original(fd) as entries:
            if isinstance(fd, int) and scan.os.fstat(fd).st_ino == inode:
                # 先返回一个目录项再抛出权限错误，构造不完整目录名单。
                def partial():
                    yield next(entries)
                    raise PermissionError('injected mid-listing failure')
                yield partial()
            else:
                yield entries
    monkeypatch.setattr(scan.os, 'scandir', interrupted)
    report = scan.scan_root(config, 'demo')
    assert report['counts']['missing'] == 0
    assert report['counts']['unavailable'] == 2
    assert 'sub' not in report['completed_directories']


# 验证单项属性读取失败仅隐藏受影响旧记录，不阻塞正常文件。
def test_stat_failure_hides_only_affected_record(authorized, monkeypatch):
    config, root = authorized
    (root / 'bad.txt').write_bytes(b'a')
    (root / 'good.txt').write_bytes(b'b')
    scan.scan_root(config, 'demo')
    original = scan.os.scandir
    class UnreadableEntry:
        name = 'bad.txt'
        # 模拟目录项属性读取失败，验证不会把该项当成已删除。
        def stat(self, **kwargs):
            raise PermissionError('injected stat failure')
    # 只为目标文件替换无法读取属性的目录项，其他条目保持真实行为。
    @contextmanager
    def fail_one(fd):
        with original(fd) as entries:
            yield (UnreadableEntry() if entry.name == 'bad.txt' else entry for entry in entries)
    monkeypatch.setattr(scan.os, 'scandir', fail_one)
    report = scan.scan_root(config, 'demo')
    rows = {r['relpath']: r for r in read_file_records(config.DATA_DIR, 'demo')}
    assert rows['bad.txt']['status'] == 'unavailable'
    assert rows['good.txt']['status'] == 'discovered'
    assert report['counts']['missing'] == 0 and not report['complete']


# 验证 SQL 中途失败使整批文件更新回滚，并保存失败报告。
def test_sql_failure_rolls_back_files_and_saves_failure_report(authorized):
    config, root = authorized
    for name in ('a.txt', 'b.txt'):
        (root / name).write_bytes(b'a')
    with sqlite3.connect(config.DATA_DIR / DB_NAME) as db:
        db.execute("""CREATE TRIGGER reject_b BEFORE INSERT ON files WHEN NEW.relpath='b.txt'
                      BEGIN SELECT RAISE(ABORT, 'injected failure'); END""")
    with pytest.raises(sqlite3.IntegrityError):
        scan.scan_root(config, 'demo')
    assert read_file_records(config.DATA_DIR, 'demo') == []
    with sqlite3.connect(config.DATA_DIR / DB_NAME) as db:
        scan_id = db.execute('SELECT id FROM jobs').fetchone()[0]
    job = read_job(config.DATA_DIR, scan_id)
    assert job['error_code'] == 'database_error'
    assert job['result']['applied'] is False


# 验证单文件超限无可用哈希且被标为不支持，其他正常文件仍登记。
def test_too_large_file_has_no_usable_hash_and_does_not_block_other_files(authorized):
    config, root = authorized
    (root / 'large.txt').write_bytes(b'12345')
    (root / 'small.txt').write_bytes(b'a')
    report = scan.scan_root(config, 'demo', max_file_bytes=2)
    rows = {r['relpath']: r for r in read_file_records(config.DATA_DIR, 'demo')}
    assert rows['large.txt']['status'] == 'unsupported' and rows['large.txt']['sha256'] is None
    assert rows['small.txt']['status'] == 'discovered'
    assert not report['complete'] and report['failed_files'][0]['sha256'] is None


# 验证提高大小上限后原超限文件恢复正常登记并沿用编号。
def test_previously_too_large_file_recovers_when_within_limit(authorized):
    config, root = authorized
    (root / 'notes.txt').write_bytes(b'12345')
    scan.scan_root(config, 'demo', max_file_bytes=2)
    before = read_file_records(config.DATA_DIR, 'demo')[0]
    assert before['status'] == 'unsupported'
    report = scan.scan_root(config, 'demo', max_file_bytes=10)
    current = read_file_records(config.DATA_DIR, 'demo')[0]
    assert report['complete'] and current['id'] == before['id']
    assert current['status'] == 'discovered'
    assert current['sha256'] == hashlib.sha256(b'12345').hexdigest()
