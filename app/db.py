"""A1 数据库结构、已知版本迁移与短事务操作。"""

from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
import re
import sqlite3

from app.config import Settings
from app.paths import checked_directory, validate_data_root


DB_NAME = 'filemind.sqlite3'
SCHEMA_VERSION = 2
# 字段中文说明使用 SQL 注释；SQLite 没有独立的列 COMMENT 元数据。
# 修改注释不升级结构版本，也不会给已有数据库自动补写建表文本中的注释。
SCAN_SCHEMA = (
    '''CREATE TABLE jobs (
        -- 任务唯一编号，当前由 UUID 生成；不是前端提交的幂等申请编号。
        id TEXT PRIMARY KEY,
        -- 任务种类，默认 scan（扫描）；当前程序仅创建 scan，数据库未限定枚举。
        kind TEXT NOT NULL DEFAULT 'scan',
        -- 任务所属授权根编号，关联 roots.id；查询报告仍须重新校验本机授权。
        root_id TEXT NOT NULL REFERENCES roots(id),
        -- queued=已登记待执行；running=执行中；succeeded=完成处理，可能存在部分失败；
        -- failed=整批失败；interrupted=服务重启时发现遗留未结束任务，不自动重放。
        state TEXT NOT NULL CHECK(state IN ('queued','running','succeeded','failed','interrupted')),
        -- 任务开始执行的次数，默认 0；开始时加 1，当前没有自动重放机制。
        attempts INTEGER NOT NULL DEFAULT 0,
        -- 错误原因，正常完整成功为 NULL；此列无枚举约束，当前写入值如下：
        -- partial_scan=部分范围或文件失败；service_restarted=重启遗留任务中断；
        -- worker_unavailable=线程池无法接单；max_files_exceeded=候选数量超限；
        -- invalid_scan_configuration=扫描配置或输入校验失败；database_error=数据库异常；
        -- source_unavailable=来源访问异常；scan_state_changed=来源、授权或快照变化；
        -- scan_aborted=其他扫描异常。单文件具体错误另见 result_json.errors。
        error_code TEXT,
        -- 任务创建时间，ISO 8601 UTC 文本。
        created_at TEXT NOT NULL,
        -- 开始执行时间，ISO 8601 UTC；尚未开始时为 NULL。
        started_at TEXT,
        -- 任务结束或被标记中断的时间，ISO 8601 UTC；未结束时为 NULL。
        finished_at TEXT,
        -- 扫描报告 JSON 文本，含计数、文件动作、跳过和错误；尚无报告时为 NULL。
        -- API 读取后将其解码为 result；重启标 interrupted 不会凭空补造扫描报告。
        result_json TEXT
    )''',
    '''CREATE TABLE idempotency (
        -- 幂等业务范围，当前仅使用 scan（扫描）；数据库未限定枚举。
        scope TEXT NOT NULL,
        -- 前端 Idempotency-Key 申请编号；与 scope 共同唯一，不等于 jobs.id。
        key TEXT NOT NULL,
        -- 规范化请求参数的 SHA256，识别同一申请编号是否被用于不同参数。
        request_hash TEXT NOT NULL,
        -- 已受理回执的 JSON，当前内容为 job_id；重复申请返回它，不重新扫描。
        response_json TEXT NOT NULL,
        PRIMARY KEY(scope,key)
    )''',
)
SCHEMA = (
    '''CREATE TABLE roots (
        -- 本机 CLI 指定的授权根编号，如 demo；不是路径，也不单独授予访问权。
        id TEXT PRIMARY KEY,
        -- 授权根的规范化绝对路径，唯一；属于本机私人配置，不向页面公开。
        path TEXT NOT NULL UNIQUE,
        -- 登记时根目录的设备号，用于检查目录所在设备是否变化。
        dev INTEGER NOT NULL,
        -- 是否启用：0=禁用，1=启用（默认）；启用仍须匹配当前本机 ROOT_PATH。
        enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
        -- 根级云许可标记：0=不允许（默认），1=许可标记；当前尚未接入云调用，
        -- 此标记不代表已经上传，也不能代替全局配置与云调用前的资料范围确认。
        cloud_allowed INTEGER NOT NULL DEFAULT 0 CHECK(cloud_allowed IN (0,1))
    )''',
    '''CREATE TABLE files (
        -- 文件身份 UUID；同内容副本不合并，符合改名证据时保留原编号。
        id TEXT PRIMARY KEY,
        -- 所属授权根编号，关联 roots.id。
        root_id TEXT NOT NULL REFERENCES roots(id),
        -- 相对于授权根的文件路径；同名文件可因目录不同而具有不同路径。
        relpath TEXT NOT NULL,
        -- 最后登记的文件设备号；故障时可能保留旧值，并非当前可用性证明。
        dev INTEGER NOT NULL,
        -- 最后登记的 inode 编号；配合设备号、哈希和唯一性证据辅助确认改名。
        ino INTEGER NOT NULL,
        -- 最后登记的文件大小，单位字节，非负；故障旧记录可保留历史属性。
        size INTEGER NOT NULL CHECK(size >= 0),
        -- 文件内容修改时间，纳秒整数；用于读取稳定性检查，不是扫描时间。
        mtime_ns INTEGER NOT NULL,
        -- 文件状态变化时间，纳秒整数；当前 Mac/Unix 语义下不是创建时间。
        ctime_ns INTEGER NOT NULL,
        -- 稳定读取后计算的 SHA256；初次未取得哈希时为 NULL。
        -- 故障旧记录可保留历史哈希，不能绕过 status 将其当作当前可用内容。
        sha256 TEXT,
        -- 内容版本，默认 1；已知哈希发生变化时递增，首次补哈希不递增。
        content_rev INTEGER NOT NULL DEFAULT 1,
        -- 路径版本，默认 1；确认改名且保留身份时递增。
        path_rev INTEGER NOT NULL DEFAULT 1,
        -- discovered=已发现，尚未完成正文处理；parsing=正文解析中（后续阶段预留）；
        -- ready=正文处理完成、可供检索使用（后续阶段预留）；
        -- stale=内容已变，旧正文或索引需更新；missing=完整枚举并复核后确认缺失；
        -- unavailable=本次无法观察或不再符合扫描边界，不能当作删除；
        -- unsupported=当前不能处理，A1 用于单文件大小超限；failed=文件读取失败；
        -- moving=文件移动处理中（B 阶段预留）；unstable=重试后读取仍不稳定。
        -- 以上是 CHECK 允许的全部值；预留值不代表对应功能已经实现。
        status TEXT NOT NULL DEFAULT 'discovered' CHECK(status IN (
            'discovered','parsing','ready','stale','missing','unavailable',
            'unsupported','failed','moving','unstable')),
        -- 首次登记此身份的时间，ISO 8601 UTC；后续扫描或保号改名不重置。
        first_seen TEXT NOT NULL
    )''',
    "CREATE UNIQUE INDEX active_file_path ON files(root_id, relpath) WHERE status != 'missing'",
)


# 生成带 UTC 时区的当前时间字符串，统一任务与记录的时间格式。
def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


# 打开独立只读数据库连接，并在读取结束后关闭连接。
@contextmanager
def read_database(data_dir: Path):
    connection = sqlite3.connect(_database_path(data_dir).as_uri() + '?mode=ro', uri=True)
    connection.row_factory = sqlite3.Row
    try:
        yield connection
    finally:
        connection.close()


# 确定应用数据库位置，并拒绝把符号链接作为数据库文件。
def _database_path(data_dir: Path) -> Path:
    path = checked_directory(data_dir, must_exist=False) / DB_NAME
    if path.is_symlink():
        raise ValueError('Database cannot be a symbolic link')
    return path


# 创建独立写连接和短事务，正常提交、异常回滚并始终关闭连接。
@contextmanager
def database_session(data_dir: Path):
    connection = sqlite3.connect(_database_path(data_dir), timeout=5)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute('PRAGMA foreign_keys=ON')
        connection.execute('PRAGMA busy_timeout=5000')
        connection.execute('PRAGMA journal_mode=WAL')
        connection.execute('BEGIN IMMEDIATE')
        with connection:
            yield connection
    finally:
        connection.close()


# 校验并登记授权根的路径和设备号，拒绝根重叠或编号换绑。
def register_root(db: sqlite3.Connection, root_id: str, root: Path, data_dir: Path):
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', root_id):
        raise ValueError('root_id must use 1-64 letters, digits, underscores or hyphens')
    _, root = validate_data_root(data_dir, root)
    for row in db.execute('SELECT id, path, dev FROM roots'):
        existing = Path(row['path'])
        if row['id'] == root_id:
            if existing != root or row['dev'] != root.stat().st_dev:
                raise ValueError('root_id is already bound to another directory or device')
            return
        if root.is_relative_to(existing) or existing.is_relative_to(root):
            raise ValueError('Authorized roots cannot overlap')
    db.execute('INSERT INTO roots(id,path,dev) VALUES(?,?,?)',
               (root_id, str(root), root.stat().st_dev))


# 创建或迁移已知数据库结构，并按本机配置登记根目录，保留既有记录。
# 调用方须持有数据目录进程锁，只迁移已知结构，不清空原有数据。
def initialize_database(configuration: Settings, *, root_id: str = 'demo') -> Path:
    # 1. 先检查路径隔离规则，通过后才创建应用数据目录。
    data = checked_directory(configuration.DATA_DIR, must_exist=False)
    if configuration.ROOT_PATH is not None:
        validate_data_root(data, configuration.ROOT_PATH)
    data.mkdir(parents=True, exist_ok=True)
    with database_session(data) as db:
        # 2. 在同一事务内读取版本；空库创建基础表，有未知表时拒绝猜测结构。
        version = db.execute('PRAGMA user_version').fetchone()[0]
        if version == 0:
            if db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchone():
                raise ValueError('Unrecognized database version: refusing existing tables')
            for statement in SCHEMA:
                db.execute(statement)
            version = 1
        if version == 1:
            # 3. 已知版本 1 仅新增扫描任务与幂等表，再升级版本，不重建原根与文件表。
            for statement in SCAN_SCHEMA:
                db.execute(statement)
            db.execute('PRAGMA user_version=2')
        elif version != SCHEMA_VERSION:
            raise ValueError(f'Unsupported database version: {version}')
        if configuration.ROOT_PATH is not None:
            # 4. 只有当前本机明确授权根时才登记，检查重叠和编号换绑后返回数据库位置。
            register_root(db, root_id, configuration.ROOT_PATH, data)
    return data / DB_NAME


# 检查已有数据库版本，迁移已知旧版并拒绝未知版本，不强制创建新库。
def check_existing_database(data_dir: Path):
    path = _database_path(data_dir)
    if not path.exists():
        return
    connection = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
    try:
        version = connection.execute('PRAGMA user_version').fetchone()[0]
        if version == 1:
            connection.close()
            initialize_database(Settings(DATA_DIR=data_dir))
        elif version != SCHEMA_VERSION:
            raise ValueError(f'Unsupported database version: {version}')
    finally:
        connection.close()


# 核对根登记与当前本机授权的路径、设备号和启用状态，返回可访问的根。
def get_registered_root(configuration: Settings, root_id: str) -> Path:
    if configuration.ROOT_PATH is None:
        raise PermissionError('ROOT_PATH has not been authorized locally')
    _, root = validate_data_root(configuration.DATA_DIR, configuration.ROOT_PATH)
    path = _database_path(configuration.DATA_DIR)
    if not path.is_file():
        raise ValueError('Initialize the database and register the root first')
    connection = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
    connection.row_factory = sqlite3.Row
    try:
        version = connection.execute('PRAGMA user_version').fetchone()[0]
        if version != SCHEMA_VERSION:
            raise ValueError(f'Unsupported database version: {version}')
        row = connection.execute('SELECT path, dev, enabled FROM roots WHERE id=?',
                                 (root_id,)).fetchone()
    finally:
        connection.close()
    if row is None:
        raise ValueError('Unknown root_id; register it through the local CLI first')
    if not row['enabled']:
        raise PermissionError('The registered root is disabled')
    if row['path'] != str(root):
        raise PermissionError('Registered root does not match the current ROOT_PATH grant')
    if row['dev'] != root.stat().st_dev:
        raise PermissionError('Registered root device has changed')
    return root


# 读取指定根的全部文件登记，作为本次身份协调的旧记录快照。
def read_file_records(data_dir: Path, root_id: str) -> list[dict]:
    connection = sqlite3.connect(_database_path(data_dir).as_uri() + '?mode=ro', uri=True)
    connection.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in connection.execute(
            'SELECT * FROM files WHERE root_id=? ORDER BY id', (root_id,))]
    finally:
        connection.close()


# 核对旧记录未被并发修改后，在当前事务中应用文件新增、更新与缺失状态。
def apply_file_records(db: sqlite3.Connection, root_id: str, *, records: list[dict],
                       missing_ids: list[str], previous: list[dict]):
    # 1. 读取当前文件表与扫描前快照比较；外部已修改时拒绝覆盖。
    current = [dict(row) for row in db.execute(
        'SELECT * FROM files WHERE root_id=? ORDER BY id', (root_id,))]
    if current != previous:
        raise RuntimeError('Database records changed during scan; run again')
    # 2. 先标记本次确认缺失的旧行，保留历史并释放这些路径的活跃唯一约束。
    for file_id in missing_ids:
        db.execute("UPDATE files SET status='missing' WHERE root_id=? AND id=?", (root_id, file_id))
    old_by_id = {row['id']: row for row in previous}
    columns = ('id', 'root_id', 'relpath', 'dev', 'ino', 'size', 'mtime_ns', 'ctime_ns',
               'sha256', 'content_rev', 'path_rev', 'status', 'first_seen')
    update_columns = columns[2:]
    # 3. 按固定列写入新行或更新改变的旧行，拒绝跨根计划；提交由外层事务统一负责。
    for row in records:
        if row['root_id'] != root_id:
            raise ValueError('Cross-root identity update is forbidden')
        old = old_by_id.get(row['id'])
        if old is None:
            db.execute(f'INSERT INTO files({",".join(columns)}) VALUES({",".join("?" for _ in columns)})',
                       tuple(row[name] for name in columns))
        elif row != old:
            # 列名来自固定常量，文件值通过参数绑定，不把用户内容拼成 SQL 代码。
            db.execute(f'UPDATE files SET {",".join(name+"=?" for name in update_columns)} WHERE root_id=? AND id=?',
                       (*[row[name] for name in update_columns], root_id, row['id']))
