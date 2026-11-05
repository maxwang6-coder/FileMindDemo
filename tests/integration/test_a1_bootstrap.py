import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from uuid import UUID

import pytest
from docx import Document
from pypdf import PdfReader

from app.config import Settings, load_settings
from app.db import DB_NAME, SCHEMA_VERSION, check_existing_database, initialize_database
from scripts.seed_demo import seed_demo
from app.scan import iter_scan_directories


# 验证重复初始化保留记录，并正确保存根目录、设备号和数据库版本。
def test_initialize_is_repeatable_and_registers_root(tmp_path):
    root = tmp_path / 'docs'
    root.mkdir()
    config = Settings(DATA_DIR=tmp_path / 'data', ROOT_PATH=root)
    path = initialize_database(config, root_id='demo')
    initialize_database(config, root_id='demo')
    with sqlite3.connect(path) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == SCHEMA_VERSION
        assert db.execute('SELECT id, path, dev, cloud_allowed FROM roots').fetchall() == [
            ('demo', str(root), root.stat().st_dev, 0)]
        assert db.execute('SELECT count(*) FROM files').fetchone()[0] == 0


# 验证未知数据库版本被拒绝且不会被初始化或启动检查改写。
def test_foreign_database_version_is_not_overwritten(tmp_path):
    config = Settings(DATA_DIR=tmp_path / 'data')
    path = initialize_database(config)
    with sqlite3.connect(path) as db:
        db.execute('PRAGMA user_version=99')
    for action in (lambda: initialize_database(config),
                   lambda: check_existing_database(config.DATA_DIR)):
        with pytest.raises(ValueError, match='version'):
            action()
    with sqlite3.connect(path) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 99


# 验证资料根内的数据目录在创建前就被拒绝，避免扫描自身。
def test_reject_data_inside_root_before_creating_it(tmp_path):
    with pytest.raises(ValueError, match='DATA_DIR'):
        initialize_database(Settings(ROOT_PATH=tmp_path, DATA_DIR=tmp_path / 'data'))
    assert not (tmp_path / 'data').exists()


# 验证配置拒绝根目录或祖先中的符号链接，包括带子路径和回退的情况。
@pytest.mark.parametrize('suffix', ['', '/child', '/../safe'])
def test_configuration_rejects_symlink_root_or_ancestor(tmp_path, suffix):
    actual = tmp_path / 'actual'
    actual.mkdir()
    (actual / 'child').mkdir()
    (tmp_path / 'safe').mkdir()
    link = tmp_path / 'link'
    link.symlink_to(actual, target_is_directory=True)
    with pytest.raises(ValueError, match='symbolic link'):
        load_settings(['--root-path', str(link) + suffix])


# 验证授权根不可重叠，已有根编号不能重新绑定到另一个目录。
def test_reject_overlapping_roots_and_rebinding_id(tmp_path):
    root = tmp_path / 'docs'
    root.mkdir()
    child = root / 'child'
    child.mkdir()
    data = tmp_path / 'data'
    initialize_database(Settings(DATA_DIR=data, ROOT_PATH=root), root_id='demo')
    with pytest.raises(ValueError, match='overlap'):
        initialize_database(Settings(DATA_DIR=data, ROOT_PATH=child), root_id='other')
    other = tmp_path / 'other'
    other.mkdir()
    with pytest.raises(ValueError, match='root_id'):
        initialize_database(Settings(DATA_DIR=data, ROOT_PATH=other), root_id='demo')


# 验证自写样例内容、UUID 清单、数据库登记与根外链接，并拒绝重复覆盖。
def test_seed_manual_fixtures_and_refuse_overwrite(tmp_path):
    config = Settings(DATA_DIR=tmp_path / 'data')
    dest = tmp_path / 'demo_docs'
    result = seed_demo(config, dest)
    manifest = json.loads((dest / 'fixture_manifest.json').read_text())
    files = manifest['files']
    assert set(f'D{i:02}' for i in range(1, 10)) <= files.keys()
    assert (dest / 'notes_01.txt').read_bytes() == (dest / 'notes_copy.txt').read_bytes()
    assert files['D01']['file_id'] != files['D06']['file_id']
    for item in files.values():
        UUID(item['file_id'])
    pdf = PdfReader(dest / 'handout.pdf')
    assert len(pdf.pages) == 2
    assert 'Eigenvalues and eigenvectors' in pdf.pages[0].extract_text()
    assert 'Worked example diagonalization' in pdf.pages[1].extract_text()
    assert [p.text for p in Document(dest / 'manual.docx').paragraphs] == [
        '第1段设备连接', '第2段 USB audio recording 无保修条款']
    assert len(PdfReader(dest / 'empty.pdf').pages) == 1
    assert PdfReader(dest / 'empty.pdf').pages[0].extract_text() == ''
    assert (dest / 'outside_link.txt').is_symlink()
    assert not (dest / 'outside_link.txt').resolve().is_relative_to(dest)
    with sqlite3.connect(config.DATA_DIR / DB_NAME) as db:
        assert db.execute('SELECT id, dev FROM roots').fetchall() == [('demo', dest.stat().st_dev)]
        rows = db.execute('SELECT id, relpath, status, sha256 FROM files').fetchall()
        assert len(rows) == len(files)
        assert {r[0] for r in rows} == {f['file_id'] for f in files.values()}
        assert all(r[2:] == ('discovered', None) for r in rows)
    loaded = load_settings(['--config', str(result['config_path'])])
    assert loaded.ROOT_PATH == dest
    assert loaded.DATA_DIR == config.DATA_DIR
    before = {p.name: p.read_bytes() for p in dest.iterdir() if p.is_file() and not p.is_symlink()}
    with pytest.raises(FileExistsError):
        seed_demo(config, dest)
    assert before == {p.name: p.read_bytes() for p in dest.iterdir() if p.is_file() and not p.is_symlink()}


# 验证边界目录已存在时拒绝生成，且不创建目标资料目录。
def test_seed_rejects_existing_boundary_without_creating_destination(tmp_path):
    (tmp_path / 'demo_docs_boundary').mkdir()
    with pytest.raises(FileExistsError):
        seed_demo(Settings(DATA_DIR=tmp_path / 'data'), tmp_path / 'demo_docs')
    assert not (tmp_path / 'demo_docs').exists()


# 验证初始化和样例生成命令可运行，重复生成明确失败且不泄露异常堆栈。
def test_bootstrap_commands_work_from_project_root(tmp_path):
    config = tmp_path / 'config.local.toml'
    config.write_text('DATA_DIR = ' + json.dumps(str(tmp_path / 'data')) + '\n')
    # 以子进程执行初始化或样例命令，收集输出并限制运行时间。
    def run(module, *args):
        return subprocess.run([sys.executable, '-m', module, '--config', str(config), *args],
                              capture_output=True, text=True, timeout=20)
    assert run('scripts.init_db').returncode == 0
    dest = str(tmp_path / 'demo_docs')
    assert run('scripts.seed_demo', '--dest', dest).returncode == 0
    duplicate = run('scripts.seed_demo', '--dest', dest)
    assert duplicate.returncode != 0
    assert 'Traceback' not in duplicate.stderr


# 验证目录遍历跳过链接、隐藏和排除目录及应用包，不进入根外。
def test_directory_boundary_skips_links_exclusions_and_packages(tmp_path):
    root = tmp_path / 'docs'
    root.mkdir()
    (root / 'allowed').mkdir()
    outside = tmp_path / 'outside'
    outside.mkdir()
    (root / 'escape').symlink_to(outside, target_is_directory=True)
    (root / 'file_link.txt').symlink_to(outside / 'does-not-exist.txt')
    for name in ('.git', '.venv', '.hidden', 'Example.app', 'Dropbox', 'CloudStorage'):
        (root / name).mkdir()
    assert list(iter_scan_directories(root)) == [root, root / 'allowed']
    assert not (outside / 'does-not-exist.txt').exists()


# 验证活跃路径唯一约束与根外键约束能拒绝不合法文件登记。
def test_file_constraints_and_foreign_keys(tmp_path):
    config = Settings(DATA_DIR=tmp_path / 'data')
    seed_demo(config, tmp_path / 'docs')
    from app.db import database_session
    with database_session(config.DATA_DIR) as db:
        row = dict(db.execute('SELECT * FROM files LIMIT 1').fetchone())
        columns = ','.join(row)
        values = ','.join('?' for _ in row)
        row['id'] = 'another-id'
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(f'INSERT INTO files({columns}) VALUES({values})', tuple(row.values()))
        row['root_id'] = 'unknown-root'
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(f'INSERT INTO files({columns}) VALUES({values})', tuple(row.values()))


# 验证服务在输出会话凭证之前拒绝未知数据库版本。
def test_startup_checks_database_before_printing_token(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app import main
    config = Settings(DATA_DIR=tmp_path / 'data')
    path = initialize_database(config)
    with sqlite3.connect(path) as db:
        db.execute('PRAGMA user_version=99')
    printed = []
    monkeypatch.setattr(main, 'print_session_token', lambda *args: printed.append(args))
    with pytest.raises(ValueError, match='version'):
        with TestClient(main.create_app(config), base_url=main.ORIGIN):
            pass
    assert printed == []
