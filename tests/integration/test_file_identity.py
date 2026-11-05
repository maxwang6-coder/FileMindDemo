import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from uuid import UUID

import pytest

from app import scan
from app.config import Settings
from app.db import DB_NAME, initialize_database
from app.process_lock import data_directory_lock
from scripts.seed_demo import seed_demo


# 在临时目录创建授权资料根与独立数据库，供文件测试使用。
@pytest.fixture
def authorized(tmp_path):
    root = tmp_path / 'documents'
    root.mkdir()
    settings = Settings(DATA_DIR=tmp_path / 'data', ROOT_PATH=root)
    initialize_database(settings, root_id='demo')
    return settings, root


# 读取测试数据库中的文件记录，供断言同步前后的变化。
def rows(settings):
    with sqlite3.connect(settings.DATA_DIR / DB_NAME) as db:
        db.row_factory = sqlite3.Row
        return [dict(r) for r in db.execute('SELECT * FROM files ORDER BY id')]


# 查找指定相对路径的非缺失记录，供测试核对文件身份。
def active(settings, relpath):
    return next(r for r in rows(settings) if r['relpath'] == relpath and r['status'] != 'missing')


# 验证新文件取得独立 UUID，相同内容不合并且重复同步不重复登记。
def test_new_files_get_uuids_and_repeat_scan_is_idempotent(authorized):
    settings, root = authorized
    for directory in ('math', 'english'):
        (root / directory).mkdir()
        (root / directory / 'notes.txt').write_bytes(b'same bytes')
    first = scan.scan_root(settings, 'demo')
    assert first['applied'] and first['complete'] and first['counts']['added'] == 2
    before = rows(settings)
    assert len({r['id'] for r in before}) == 2
    for r in before:
        UUID(r['id'])
        assert r['sha256'] == hashlib.sha256(b'same bytes').hexdigest()
        assert r['status'] == 'discovered'
    second = scan.scan_root(settings, 'demo')
    assert second['counts']['unchanged'] == 2 and second['counts']['added'] == 0
    assert rows(settings) == before


# 验证同路径内容改变保留编号并增加内容版本，使旧就绪状态失效。
def test_same_path_content_change_keeps_id_and_invalidates_ready(authorized):
    settings, root = authorized
    path = root / 'notes.txt'
    path.write_bytes(b'old')
    scan.scan_root(settings, 'demo')
    original = active(settings, 'notes.txt')
    with sqlite3.connect(settings.DATA_DIR / DB_NAME) as db:
        db.execute("UPDATE files SET status='ready'")
    path.write_bytes(b'new content')
    result = scan.scan_root(settings, 'demo')
    current = active(settings, 'notes.txt')
    assert result['counts']['modified'] == 1
    assert current['id'] == original['id']
    assert current['first_seen'] == original['first_seen']
    assert current['content_rev'] == original['content_rev'] + 1
    assert current['path_rev'] == original['path_rev']
    assert current['status'] == 'stale'
    assert current['sha256'] == hashlib.sha256(b'new content').hexdigest()


# 验证仅文件时间变化时更新元数据，不增加内容或路径版本。
def test_only_timestamp_change_does_not_change_content_version(authorized):
    settings, root = authorized
    path = root / 'notes.txt'
    path.write_bytes(b'unchanged')
    scan.scan_root(settings, 'demo')
    before = active(settings, 'notes.txt')
    info = path.stat()
    os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns + 1_000_000))
    report = scan.scan_root(settings, 'demo')
    current = active(settings, 'notes.txt')
    assert report['counts']['metadata_updated'] == 1
    assert (current['id'], current['content_rev'], current['path_rev']) == (
        before['id'], before['content_rev'], before['path_rev'])


# 验证唯一同设备、inode 和哈希匹配的改名保留编号并增加路径版本。
def test_unique_same_device_inode_hash_rename_keeps_id(authorized):
    settings, root = authorized
    path = root / 'before.txt'
    path.write_bytes(b'lesson')
    scan.scan_root(settings, 'demo')
    before = active(settings, 'before.txt')
    path.rename(root / 'after.txt')
    result = scan.scan_root(settings, 'demo')
    current = active(settings, 'after.txt')
    assert result['counts']['renamed'] == 1 and result['counts']['missing'] == 0
    assert len(rows(settings)) == 1
    assert current['id'] == before['id']
    assert current['path_rev'] == before['path_rev'] + 1
    assert current['content_rev'] == before['content_rev']


# 验证同内容副本取得新编号，原文件编号和相同内容哈希分别保留。
def test_copy_does_not_merge_same_hash(authorized):
    settings, root = authorized
    path = root / 'notes.txt'
    path.write_bytes(b'lesson')
    scan.scan_root(settings, 'demo')
    original = active(settings, 'notes.txt')
    shutil.copyfile(path, root / 'copy.txt')
    scan.scan_root(settings, 'demo')
    copy = active(settings, 'copy.txt')
    assert copy['id'] != original['id']
    assert copy['sha256'] == original['sha256']
    assert active(settings, 'notes.txt')['id'] == original['id']


# 验证复制后删除原件不能只凭相同哈希推断改名，旧项缺失、新项新号。
def test_copy_then_remove_original_is_not_inferred_as_rename(authorized):
    settings, root = authorized
    path = root / 'before.txt'
    path.write_bytes(b'lesson')
    scan.scan_root(settings, 'demo')
    original = active(settings, 'before.txt')
    shutil.copyfile(path, root / 'after.txt')
    path.unlink()
    result = scan.scan_root(settings, 'demo')
    assert result['counts']['missing'] == 1 and result['counts']['added'] == 1
    assert result['counts']['renamed'] == 0
    assert active(settings, 'after.txt')['id'] != original['id']
    assert next(r for r in rows(settings) if r['id'] == original['id'])['status'] == 'missing'


# 验证改名同时改变内容时证据不足，不沿用旧编号。
def test_rename_with_changed_content_is_new_identity(authorized):
    settings, root = authorized
    path = root / 'before.txt'
    path.write_bytes(b'old')
    scan.scan_root(settings, 'demo')
    original = active(settings, 'before.txt')
    path.rename(root / 'after.txt')
    (root / 'after.txt').write_bytes(b'edited too')
    result = scan.scan_root(settings, 'demo')
    assert result['counts']['missing'] == 1 and result['counts']['added'] == 1
    assert active(settings, 'after.txt')['id'] != original['id']


# 验证根内硬链接造成匹配歧义时拒绝保留改名身份。
def test_hard_link_ambiguity_does_not_preserve_rename_id(authorized):
    settings, root = authorized
    path = root / 'one.txt'
    path.write_bytes(b'same inode')
    os.link(path, root / 'two.txt')
    scan.scan_root(settings, 'demo')
    second_id = active(settings, 'two.txt')['id']
    (root / 'two.txt').rename(root / 'three.txt')
    report = scan.scan_root(settings, 'demo')
    assert report['counts']['renamed'] == 0 and report['counts']['missing'] == 1
    assert active(settings, 'three.txt')['id'] != second_id


# 验证存在根外硬链接时也不把单个可见新路径直接认作唯一改名。
def test_outside_hard_link_also_blocks_rename_inference(authorized, tmp_path):
    settings, root = authorized
    path = root / 'before.txt'
    path.write_bytes(b'same inode')
    os.link(path, tmp_path / 'outside.txt')
    scan.scan_root(settings, 'demo')
    original = active(settings, 'before.txt')
    path.rename(root / 'after.txt')
    report = scan.scan_root(settings, 'demo')
    assert report['counts']['renamed'] == 0
    assert active(settings, 'after.txt')['id'] != original['id']


# 验证已缺失的历史路径再次出现时创建新身份，不仅凭同名复活旧行。
def test_missing_file_reappears_as_new_identity(authorized):
    settings, root = authorized
    path = root / 'notes.txt'
    path.write_bytes(b'old')
    scan.scan_root(settings, 'demo')
    original = active(settings, 'notes.txt')
    path.unlink()
    assert scan.scan_root(settings, 'demo')['counts']['missing'] == 1
    path.write_bytes(b'new')
    scan.scan_root(settings, 'demo')
    assert active(settings, 'notes.txt')['id'] != original['id']
    assert len(rows(settings)) == 2


# 验证单文件超限不妨碍成功范围确认缺失，失败项仍无可用哈希。
def test_failed_hash_does_not_block_missing_in_complete_scope(authorized):
    settings, root = authorized
    path = root / 'old.txt'
    path.write_bytes(b'a')
    scan.scan_root(settings, 'demo')
    original = active(settings, 'old.txt')
    path.unlink()
    (root / 'large.txt').write_bytes(b'abc')
    result = scan.scan_root(settings, 'demo', max_file_bytes=2)
    assert result['applied'] and not result['complete']
    assert result['counts']['missing'] == 1 and result['counts']['failed'] == 1
    assert next(r for r in rows(settings) if r['id'] == original['id'])['status'] == 'missing'
    assert active(settings, 'large.txt')['status'] == 'unsupported'
    assert active(settings, 'large.txt')['sha256'] is None


# 验证正式扫描中原文件被链接替换时标不可用，保留编号且不误标缺失。
def test_replaced_by_symlink_is_not_misreported_as_missing(authorized, tmp_path):
    settings, root = authorized
    path = root / 'notes.txt'
    path.write_bytes(b'a')
    scan.scan_root(settings, 'demo')
    before = rows(settings)
    path.unlink()
    outside = tmp_path / 'outside.txt'
    outside.write_bytes(b'outside')
    path.symlink_to(outside)
    result = scan.scan_root(settings, 'demo')
    assert result['applied'] and not result['complete']
    assert result['counts']['missing'] == 0 and result['counts']['unavailable'] == 1
    assert rows(settings) == [dict(before[0], status='unavailable')]
    assert result['files'] == []


# 验证哈希完成后源文件再次变化时拒绝提交，旧记录不变。
def test_change_after_hash_recheck_aborts_commit(authorized, monkeypatch):
    settings, root = authorized
    path = root / 'notes.txt'
    path.write_bytes(b'old')
    scan.scan_root(settings, 'demo')
    before = rows(settings)
    original = scan._hash_file
    # 单文件哈希完成后修改源文件，验证正式扫描提交前仍会复核。
    def change_after_hash(*args, **kwargs):
        report = original(*args, **kwargs)
        path.write_bytes(b'new content after stable hash')
        return report
    monkeypatch.setattr(scan, '_hash_file', change_after_hash)
    with pytest.raises(RuntimeError, match='changed'):
        scan.scan_root(settings, 'demo')
    assert rows(settings) == before


# 验证样例首次同步补齐哈希，同时保留清单 UUID 和清单内容。
def test_seed_manifest_ids_are_preserved_on_first_sync(tmp_path):
    config = Settings(DATA_DIR=tmp_path / 'data')
    root = tmp_path / 'demo'
    seed_demo(config, root)
    settings = Settings(DATA_DIR=config.DATA_DIR, ROOT_PATH=root)
    manifest = json.loads((root / 'fixture_manifest.json').read_text())
    before = rows(settings)
    report = scan.scan_root(settings, 'demo')
    assert report['counts']['initialized'] == 13 and report['counts']['added'] == 0
    assert {r['id'] for r in rows(settings)} == {r['id'] for r in before}
    assert {r['id'] for r in rows(settings)} == {f['file_id'] for f in manifest['files'].values()}
    assert all(r['sha256'] and r['content_rev'] == 1 for r in rows(settings))
    assert json.loads((root / 'fixture_manifest.json').read_text()) == manifest


# 验证数据目录被其他使用者持锁时拒绝身份同步且不新增文件记录。
def test_existing_process_lock_prevents_sync(authorized):
    settings, root = authorized
    (root / 'notes.txt').write_bytes(b'a')
    with data_directory_lock(settings.DATA_DIR):
        with pytest.raises(RuntimeError, match='DATA_DIR'):
            scan.scan_root(settings, 'demo')
    assert rows(settings) == []


# 验证只读 CLI 模式不写文件登记，正式同步模式才应用身份结果。
def test_cli_readonly_modes_do_not_write_and_full_scan_syncs(authorized, tmp_path):
    settings, root = authorized
    (root / 'notes.txt').write_bytes(b'a')
    config = tmp_path / 'config.local.toml'
    config.write_text('DATA_DIR = ' + json.dumps(str(settings.DATA_DIR)) + '\nROOT_PATH = '
                      + json.dumps(str(root)) + '\n')
    command = [sys.executable, '-m', 'scripts.scan', '--config', str(config), '--root-id', 'demo']
    for flags in (['--metadata-only'], ['--verify-content']):
        result = subprocess.run(command + flags, capture_output=True, text=True, timeout=10)
        assert result.returncode == 0, result.stdout + result.stderr
        assert rows(settings) == []
    result = subprocess.run(command + ['--sync'], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report['mode'] == 'root_scan' and report['applied'] is True
    assert report['counts']['added'] == 1
    assert len(rows(settings)) == 1


# 验证只改变文件名大小写时可识别合格改名并保留原编号。
def test_case_only_rename_preserves_identity(authorized):
    settings, root = authorized
    (root / 'notes.txt').write_bytes(b'lesson')
    scan.scan_root(settings, 'demo')
    original = active(settings, 'notes.txt')
    (root / 'notes.txt').rename(root / 'Notes.txt')
    report = scan.scan_root(settings, 'demo')
    assert report['counts']['renamed'] == 1
    assert active(settings, 'Notes.txt')['id'] == original['id']


# 验证当前本机授权改为其他目录时不能同步原根编号。
def test_different_current_grant_cannot_sync_registered_root(authorized, tmp_path):
    settings, root = authorized
    other = tmp_path / 'other'
    other.mkdir()
    (other / 'secret.txt').write_bytes(b'not authorized for this root id')
    configuration = Settings(DATA_DIR=settings.DATA_DIR, ROOT_PATH=other)
    with pytest.raises(PermissionError, match='grant'):
        scan.scan_root(configuration, 'demo')
    assert rows(settings) == []


# 验证扫描计划生成后数据库被外部修改时拒绝覆盖该修改。
def test_concurrent_database_change_is_not_overwritten(authorized, monkeypatch):
    settings, root = authorized
    (root / 'notes.txt').write_bytes(b'a')
    scan.scan_root(settings, 'demo')
    original = scan.plan_identity_changes
    # 生成身份计划后直接修改数据库，模拟提交前的记录并发变化。
    def change_after_plan(*args, **kwargs):
        plan = original(*args, **kwargs)
        with sqlite3.connect(settings.DATA_DIR / DB_NAME) as db:
            db.execute('UPDATE files SET content_rev=99')
        return plan
    monkeypatch.setattr(scan, 'plan_identity_changes', change_after_plan)
    with pytest.raises(RuntimeError, match='Database records changed'):
        scan.scan_root(settings, 'demo')
    assert active(settings, 'notes.txt')['content_rev'] == 99


# 分别验证设备号、inode 或旧哈希证据不足时不保留改名身份。
@pytest.mark.parametrize('column', ['dev', 'ino', 'sha256'])
def test_rename_requires_device_inode_and_prior_hash(authorized, column):
    settings, root = authorized
    (root / 'before.txt').write_bytes(b'lesson')
    scan.scan_root(settings, 'demo')
    original = active(settings, 'before.txt')
    # 只改变数据库中的一项匹配证据，分别验证改名保号的每个前提。
    with sqlite3.connect(settings.DATA_DIR / DB_NAME) as db:
        if column == 'sha256':
            db.execute('UPDATE files SET sha256=NULL')
        elif column == 'dev':
            db.execute('UPDATE files SET dev=dev+1')
        else:
            db.execute('UPDATE files SET ino=ino+1')
    (root / 'before.txt').rename(root / 'after.txt')
    report = scan.scan_root(settings, 'demo')
    assert report['counts']['renamed'] == 0
    assert report['counts']['missing'] == 1 and report['counts']['added'] == 1
    assert active(settings, 'after.txt')['id'] != original['id']
