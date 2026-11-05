from dataclasses import asdict
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from app.config import Settings
from app.db import DB_NAME, initialize_database
from app import scan


# 在临时目录创建授权资料根与独立数据库，供文件测试使用。
@pytest.fixture
def authorized(tmp_path):
    root = tmp_path / 'documents'
    root.mkdir()
    settings = Settings(DATA_DIR=tmp_path / 'data', ROOT_PATH=root)
    initialize_database(settings, root_id='demo')
    return settings, root


# 验证已知内容和空文件的 SHA256 正确，且首次稳定读取无需重试。
def test_known_sha256_and_empty_file(authorized):
    settings, root = authorized
    (root / 'abc.txt').write_bytes(b'abc')
    (root / 'empty.txt').write_bytes(b'')
    report = scan.collect_file_hashes(settings, 'demo')
    results = {r.relpath: r for r in report.files}
    assert report.complete is True
    assert results['abc.txt'].sha256 == 'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad'
    assert results['empty.txt'].sha256 == 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855'
    assert all(r.status == 'stable' and r.attempts == 1 for r in report.files)


# 验证哈希读取器每次请求 1MiB，并正确累计摘要与总字节数。
def test_reader_always_requests_one_mib_chunks():
    content = b'a' * (2 * 1024 * 1024 + 7)
    requests = []
    class Reader(io.BytesIO):
        # 记录每次读取请求的块大小，再读取内存字节流供分块测试断言。
        def read(self, size=-1):
            requests.append(size)
            return super().read(size)
    digest, count = scan._read_sha256(Reader(content), max_bytes=len(content))
    assert count == len(content)
    assert digest == hashlib.sha256(content).hexdigest()
    assert requests == [1024 * 1024] * 4  # 三次读取数据块，最后一次读取确认结束。


# 验证相同内容副本哈希相同，但严格哈希报告仍保留两个独立观察。
def test_copies_share_hash_but_remain_separate_observations(authorized):
    settings, root = authorized
    for name in ('a.txt', 'b.txt'):
        (root / name).write_bytes(b'identical')
    report = scan.collect_file_hashes(settings, 'demo')
    assert len(report.files) == 2
    assert report.files[0].sha256 == report.files[1].sha256
    assert 'file_id' not in asdict(report.files[0])


# 验证读取后内容变化时丢弃首次哈希，延迟重试后使用稳定新内容。
def test_mutation_discards_first_digest_and_retries_once(authorized, monkeypatch):
    settings, root = authorized
    path = root / 'notes.txt'
    path.write_bytes(b'old')
    original = scan._read_sha256
    calls = []
    # 首次读取后修改临时文件内容，触发稳定性检查失败并验证重试。
    def change_after_read(stream, *, max_bytes):
        result = original(stream, max_bytes=max_bytes)
        calls.append(1)
        if len(calls) == 1:
            path.write_bytes(b'new, stable bytes')
        return result
    monkeypatch.setattr(scan, '_read_sha256', change_after_read)
    sleeps = []
    result = scan.collect_file_hashes(settings, 'demo', sleep=sleeps.append).files[0]
    assert result.status == 'stable' and result.attempts == 2
    assert result.sha256 == hashlib.sha256(b'new, stable bytes').hexdigest()
    assert result.size == len(b'new, stable bytes')
    assert sleeps == [scan.HASH_RETRY_DELAY_SECONDS]


# 验证两次读取均变化时返回不稳定状态，不提供可用哈希。
def test_continuous_mutation_has_no_usable_hash(authorized, monkeypatch):
    settings, root = authorized
    path = root / 'notes.txt'
    path.write_bytes(b'original')
    original = scan._read_sha256
    # 每次读取后继续修改临时文件，模拟无法稳定读取的来源。
    def always_change(stream, *, max_bytes):
        result = original(stream, max_bytes=max_bytes)
        with path.open('ab') as output:
            output.write(b'x')
        return result
    monkeypatch.setattr(scan, '_read_sha256', always_change)
    sleeps = []
    report = scan.collect_file_hashes(settings, 'demo', sleep=sleeps.append)
    result = report.files[0]
    assert result.status == 'unstable' and result.sha256 is None
    assert result.attempts == 2 and len(sleeps) == 1
    assert report.complete is False


# 验证路径被同内容不同底层对象替换时仍能发现变化并重试。
def test_replacing_path_with_same_bytes_is_still_detected(authorized, monkeypatch):
    settings, root = authorized
    path = root / 'notes.txt'
    path.write_bytes(b'abc')
    old_inode = path.stat().st_ino
    original = scan._read_sha256
    calls = []
    # 首次读取后用相同字节的新文件替换原路径，模拟底层文件身份变化。
    def replace_after_read(stream, *, max_bytes):
        result = original(stream, max_bytes=max_bytes)
        calls.append(1)
        if len(calls) == 1:
            replacement = root / '.replacement'
            replacement.write_bytes(b'abc')
            path.unlink()
            replacement.rename(path)
        return result
    monkeypatch.setattr(scan, '_read_sha256', replace_after_read)
    result = scan.collect_file_hashes(settings, 'demo', sleep=lambda _: None).files[0]
    assert result.status == 'stable' and result.attempts == 2
    assert result.st_ino == path.stat().st_ino and result.st_ino != old_inode


# 验证读取后源路径换成根外链接时不会对外部目标计算哈希。
def test_link_swap_after_read_never_hashes_outside_target(authorized, tmp_path, monkeypatch):
    settings, root = authorized
    path = root / 'notes.txt'
    path.write_bytes(b'allowed')
    outside = tmp_path / 'outside.txt'
    outside.write_bytes(b'must never read')
    original = scan._read_sha256
    calls = []
    # 读取后把源文件换成根外符号链接，模拟文件路径变化。
    def swap_after_read(stream, *, max_bytes):
        result = original(stream, max_bytes=max_bytes)
        calls.append(1)
        path.unlink()
        path.symlink_to(outside)
        return result
    monkeypatch.setattr(scan, '_read_sha256', swap_after_read)
    result = scan.collect_file_hashes(settings, 'demo', sleep=lambda _: None).files[0]
    assert result.status == 'failed' and result.sha256 is None
    assert calls == [1]


# 验证父目录被替换成链接时拒绝继续沿新路径读取根外文件。
def test_parent_link_swap_never_opens_outside_target(authorized, tmp_path, monkeypatch):
    settings, root = authorized
    folder = root / 'folder'
    folder.mkdir()
    (folder / 'notes.txt').write_bytes(b'allowed')
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'notes.txt').write_bytes(b'must never read')
    original = scan._read_sha256
    calls = []
    # 读取后把父目录换成根外符号链接，模拟祖先路径变化。
    def swap_after_read(stream, *, max_bytes):
        result = original(stream, max_bytes=max_bytes)
        calls.append(1)
        folder.rename(root / '.original-folder')
        folder.symlink_to(outside, target_is_directory=True)
        return result
    monkeypatch.setattr(scan, '_read_sha256', swap_after_read)
    result = scan.collect_file_hashes(settings, 'demo', sleep=lambda _: None).files[0]
    assert result.status == 'failed' and result.sha256 is None
    assert calls == [1]


# 验证文件被替换为命名管道时安全拒绝，不因打开管道阻塞扫描。
def test_fifo_swap_before_open_does_not_block(authorized, monkeypatch):
    settings, root = authorized
    path = root / 'notes.txt'
    path.write_bytes(b'allowed')
    original = scan._open_hash_source
    first = True
    # 在读取函数打开文件之前换成命名管道，验证不会阻塞。
    def swap_before_open(root_path, relpath):
        nonlocal first
        if first:
            first = False
            path.unlink()
            os.mkfifo(path)
        return original(root_path, relpath)
    monkeypatch.setattr(scan, '_open_hash_source', swap_before_open)
    result = scan.collect_file_hashes(settings, 'demo', sleep=lambda _: None).files[0]
    assert result.status == 'failed' and result.sha256 is None


# 验证预先已知超限的文件不读取内容、不重试且无可用哈希。
def test_size_limit_rejects_without_reading_or_sleeping(authorized, monkeypatch):
    settings, root = authorized
    (root / 'notes.txt').write_bytes(b'abc')
    # 若超限文件触发读取或等待则让测试失败，确认提前拒绝。
    def forbidden(*args, **kwargs):
        raise AssertionError('Oversize file must not be read or retried')
    monkeypatch.setattr(scan, '_read_sha256', forbidden)
    result = scan.collect_file_hashes(settings, 'demo', max_file_bytes=2, sleep=forbidden).files[0]
    assert result.status == 'too_large' and result.sha256 is None


# 验证严格哈希检查不改变源文件属性或数据库登记。
def test_hashing_preserves_sources_and_database(authorized):
    settings, root = authorized
    path = root / 'notes.txt'
    path.write_bytes(b'preserve me')
    before = path.stat()
    # 读取根与文件表内容，比较哈希检查前后数据库是否保持不变。
    def records():
        with sqlite3.connect(settings.DATA_DIR / DB_NAME) as db:
            return db.execute('SELECT * FROM roots').fetchall(), db.execute('SELECT * FROM files').fetchall()
    before_db = records()
    scan.collect_file_hashes(settings, 'demo')
    assert records() == before_db
    assert path.read_bytes() == b'preserve me'
    assert (path.stat().st_size, path.stat().st_mtime_ns, path.stat().st_ctime_ns) == (
        before.st_size, before.st_mtime_ns, before.st_ctime_ns)


# 验证显式哈希检查与元数据检查命令分别返回对应报告模式。
def test_verify_content_cli_is_opt_in(authorized, tmp_path):
    settings, root = authorized
    (root / 'notes.txt').write_bytes(b'abc')
    config = tmp_path / 'config.local.toml'
    config.write_text('DATA_DIR = ' + json.dumps(str(settings.DATA_DIR)) + '\nROOT_PATH = '
                      + json.dumps(str(root)) + '\n')
    base = [sys.executable, '-m', 'scripts.scan', '--config', str(config), '--root-id', 'demo']
    run = subprocess.run(base + ['--verify-content'], capture_output=True, text=True, timeout=10)
    assert run.returncode == 0, run.stdout + run.stderr
    result = json.loads(run.stdout)
    assert result['mode'] == 'content_hashes' and result['complete'] is True
    assert result['files'][0]['sha256'] == 'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad'
    run = subprocess.run(base + ['--metadata-only'], capture_output=True, text=True, timeout=10)
    assert run.returncode == 0
    assert json.loads(run.stdout)['mode'] == 'metadata_only'


# 验证枚举后文件已变化时先延迟重试，再读取新的稳定版本。
def test_change_since_enumeration_retries_before_reading(authorized, monkeypatch):
    settings, root = authorized
    path = root / 'notes.txt'
    path.write_bytes(b'old')
    original_listing = scan.collect_file_metadata
    # 枚举得到旧快照后修改文件，模拟枚举与打开之间的版本变化。
    def update_after_listing(*args):
        report = original_listing(*args)
        path.write_bytes(b'new content')
        return report
    monkeypatch.setattr(scan, 'collect_file_metadata', update_after_listing)
    original_read = scan._read_sha256
    reads = []
    # 记录实际读取次数，再调用原哈希读取器。
    def count_reads(stream, *, max_bytes):
        reads.append(1)
        return original_read(stream, max_bytes=max_bytes)
    monkeypatch.setattr(scan, '_read_sha256', count_reads)
    result = scan.collect_file_hashes(settings, 'demo', sleep=lambda _: None).files[0]
    assert result.status == 'stable' and result.attempts == 2
    assert reads == [1]
    assert result.sha256 == hashlib.sha256(b'new content').hexdigest()


# 验证大小和修改时间保持不变时，仍通过 ctime 发现内容变化。
def test_same_size_and_preserved_mtime_still_checks_ctime(authorized, monkeypatch):
    settings, root = authorized
    path = root / 'notes.txt'
    path.write_bytes(b'old')
    original_read = scan._read_sha256
    calls = []
    # 改写文件但保留大小和修改时间，验证变化仍能通过 ctime 被检测。
    def rewrite_preserving_mtime(stream, *, max_bytes):
        result = original_read(stream, max_bytes=max_bytes)
        calls.append(1)
        if len(calls) == 1:
            before = path.stat()
            path.write_bytes(b'new')
            os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        return result
    monkeypatch.setattr(scan, '_read_sha256', rewrite_preserving_mtime)
    result = scan.collect_file_hashes(settings, 'demo', sleep=lambda _: None).files[0]
    assert result.status == 'stable' and result.attempts == 2
    assert result.sha256 == hashlib.sha256(b'new').hexdigest()


# 验证单文件不可读时保留其他稳定结果，但报告明确不完整。
def test_unreadable_file_keeps_other_results_but_report_is_incomplete(authorized, monkeypatch):
    settings, root = authorized
    (root / 'a.txt').write_bytes(b'a')
    (root / 'b.txt').write_bytes(b'b')
    original = scan._open_hash_source
    # 仅对指定文件模拟读取权限失败，其他候选正常读取。
    def deny_one(root_path, relpath):
        if relpath == 'a.txt':
            raise PermissionError('simulated permission failure')
        return original(root_path, relpath)
    monkeypatch.setattr(scan, '_open_hash_source', deny_one)
    report = scan.collect_file_hashes(settings, 'demo')
    assert report.complete is False
    assert report.files[0].status == 'failed' and report.files[0].sha256 is None
    assert report.files[1].status == 'stable' and report.files[1].sha256 is not None


# 验证文件在读取期间增长超限时不给出截断内容的哈希。
def test_growing_past_limit_has_no_digest(authorized, monkeypatch):
    settings, root = authorized
    path = root / 'notes.txt'
    path.write_bytes(b'abc')
    original = scan._read_sha256
    # 在真正读取前追加字节，使文件增长超过配置上限。
    def grow_before_read(stream, *, max_bytes):
        path.write_bytes(b'a' * 8)
        return original(stream, max_bytes=max_bytes)
    monkeypatch.setattr(scan, '_read_sha256', grow_before_read)
    result = scan.collect_file_hashes(settings, 'demo', max_file_bytes=5).files[0]
    assert result.status == 'too_large' and result.sha256 is None


# 验证哈希 CLI 对超限文件返回非零退出码和无哈希的明确失败项。
def test_cli_oversize_reports_failure_without_truncating(authorized, tmp_path):
    settings, root = authorized
    with (root / 'large.txt').open('wb') as output:
        output.truncate(1024 * 1024 + 1)
    config = tmp_path / 'config.local.toml'
    config.write_text('DATA_DIR = ' + json.dumps(str(settings.DATA_DIR)) + '\nROOT_PATH = '
                      + json.dumps(str(root)) + '\n')
    command = [sys.executable, '-m', 'scripts.scan', '--config', str(config),
               '--root-id', 'demo', '--verify-content', '--max-file-mib', '1']
    run = subprocess.run(command, capture_output=True, text=True, timeout=10)
    assert run.returncode != 0
    result = json.loads(run.stdout)
    assert result['complete'] is False and result['stable_count'] == 0
    assert result['files'][0]['status'] == 'too_large'
    assert result['files'][0]['sha256'] is None
    assert result['max_file_bytes'] == 1024 * 1024
