"""A1 元数据与哈希检查、文件身份协调和范围扫描；始终只读源资料。"""

from collections import Counter
from contextlib import contextmanager, nullcontext
from dataclasses import asdict, dataclass
import errno
import hashlib
import os
from pathlib import Path
import stat
import sqlite3
import time
from uuid import uuid4

from app.config import Settings
from app.db import apply_file_records, database_session, get_registered_root, read_file_records, utc_now
from app.identity import plan_identity_changes
from app.paths import checked_directory
from app.process_lock import data_directory_lock


EXCLUDED_NAMES = frozenset({
    'node_modules', '__pycache__', 'Applications', 'System', 'Library',
    'CloudStorage', 'Mobile Documents', 'Dropbox', 'Google Drive', 'OneDrive',
})
EXCLUDED_SUFFIXES = ('.app', '.photoslibrary', '.icloud')
ALLOWED_EXTENSIONS = frozenset({'.txt', '.md', '.pdf', '.docx'})
HASH_CHUNK_BYTES = 1024 * 1024
MAX_HASH_BYTES = 20 * 1024 * 1024
HASH_RETRY_DELAY_SECONDS = 1.0


# 根据名称和后缀判断是否属于隐藏项、依赖目录、应用包或常见云目录。
def _excluded(path: Path) -> bool:
    return (path.name.startswith('.') or path.name in EXCLUDED_NAMES
            or path.name.lower().startswith('onedrive - ')
            or path.name.lower().endswith(EXCLUDED_SUFFIXES))


# 遍历允许进入的同设备目录，不跟随链接，遇到遍历错误向上传递。
# 仅枚举目录，不读正文或写数据库；错误向上传递，此边界不防御同账号恶意竞态。
def iter_scan_directories(root: Path):
    root = checked_directory(root)
    if any(_excluded(p) for p in (root, *root.parents) if p.name):
        raise ValueError('Authorized root is inside an excluded directory')
    if root == Path('/') or root.is_relative_to('/Volumes') or root.is_relative_to('/Network'):
        raise ValueError('System, external and network roots are excluded')
    device = root.stat().st_dev
    pending = [root]
    while pending:
        directory = pending.pop()
        # 打开目录前再次检查原始路径，不能先解析链接再进入目标。
        checked_directory(directory)
        if directory.stat().st_dev != device:
            continue
        yield directory
        with os.scandir(directory) as entries:
            children = [Path(entry.path) for entry in entries
                        if not _excluded(Path(entry.path))
                        and not entry.is_symlink()
                        and entry.is_dir(follow_symlinks=False)]
        pending.extend(sorted(children, reverse=True))


@dataclass(frozen=True, slots=True)
class FileMetadata:
    """文件系统的一次观察记录，不代表文件身份或可搜索文档。"""

    relpath: str
    size: int
    mtime_ns: int
    ctime_ns: int
    st_dev: int
    st_ino: int


@dataclass(frozen=True, slots=True)
class MetadataReport:
    root_id: str
    files: list[FileMetadata]
    skipped: dict[str, int]
    mode: str = 'metadata_only'
    complete: bool = True


# 严格清点白名单普通文件的相对路径和属性，不读正文也不更新文件登记。
# 任意遍历或属性读取错误会中止严格检查；范围级故障隔离由正式扫描入口处理。
def collect_file_metadata(configuration: Settings, root_id: str) -> MetadataReport:
    root = get_registered_root(configuration, root_id)
    if type(configuration.MAX_FILES) is not int or configuration.MAX_FILES <= 0:
        raise ValueError('MAX_FILES must be a positive integer')
    device = root.stat().st_dev
    files = []
    skipped = Counter()
    for directory in iter_scan_directories(root):
        checked_directory(directory)
        with os.scandir(directory) as entries:
            for entry in sorted(entries, key=lambda item: item.name):
                path = directory / entry.name
                if _excluded(path):
                    skipped['excluded_name'] += 1
                    continue
                info = path.lstat()  # 检查目录项本身，不读取符号链接目标的属性。
                if stat.S_ISLNK(info.st_mode):
                    skipped['symbolic_link'] += 1
                    continue
                if stat.S_ISDIR(info.st_mode):
                    continue  # 子目录由目录迭代器负责遍历，此处不重复进入。
                if not stat.S_ISREG(info.st_mode):
                    skipped['not_regular_file'] += 1
                    continue
                if info.st_dev != device:
                    skipped['different_device'] += 1
                    continue
                if path.suffix.lower() not in ALLOWED_EXTENSIONS:
                    skipped['extension_not_allowed'] += 1
                    continue
                if len(files) >= configuration.MAX_FILES:
                    raise ValueError('MAX_FILES exceeded; collection aborted without DB changes')
                files.append(FileMetadata(
                    relpath=path.relative_to(root).as_posix(), size=info.st_size,
                    mtime_ns=info.st_mtime_ns, ctime_ns=info.st_ctime_ns,
                    st_dev=info.st_dev, st_ino=info.st_ino))
    return MetadataReport(root_id=root_id, files=files, skipped=dict(skipped))


@dataclass(frozen=True, slots=True)
class FileHashResult(FileMetadata):
    sha256: str | None
    status: str
    attempts: int
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class HashReport:
    root_id: str
    files: list[FileHashResult]
    skipped: dict[str, int]
    complete: bool
    mode: str = 'content_hashes'


class _FileChanged(Exception):
    """读取版本发生变化，需要丢弃本次摘要并至多重试一次。"""


class _FileTooLarge(ValueError):
    pass


# 把一次文件系统状态转换为包含相对路径的统一元数据对象。
def _metadata(relpath: str, info: os.stat_result) -> FileMetadata:
    return FileMetadata(relpath=relpath, size=info.st_size,
                        mtime_ns=info.st_mtime_ns, ctime_ns=info.st_ctime_ns,
                        st_dev=info.st_dev, st_ino=info.st_ino)


# 提取设备号、inode、大小和时间，组成检测文件变化的比较签名。
def _signature(info: FileMetadata) -> tuple:
    return (info.st_dev, info.st_ino, info.size, info.mtime_ns, info.ctime_ns)


# 通过逐层不跟随链接的描述符只读打开文件，供后续身份和内容校验。
# 最终使用非阻塞打开，避免文件换成管道后卡住；调用方须在读内容前确认仍是普通文件。
@contextmanager
def _open_hash_source(root: Path, relpath: str):
    relative = Path(relpath)
    if relative.is_absolute() or not relative.parts or '..' in relative.parts:
        raise PermissionError('Invalid relative source path')
    if any(_excluded(Path(part)) for part in relative.parts):
        raise PermissionError('Excluded source path')
    checked_directory(root)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    directory_fd = os.open(root, flags)
    try:
        for part in relative.parts[:-1]:
            child_fd = os.open(part, flags, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = child_fd
        fd = os.open(relative.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                     dir_fd=directory_fd)
        with os.fdopen(fd, 'rb') as stream:
            yield stream
    finally:
        os.close(directory_fd)


# 每次按 1MiB 读取并累积 SHA256，同时统计字节并拒绝超过读取上限。
def _read_sha256(stream, *, max_bytes: int) -> tuple[str, int]:
    digest = hashlib.sha256()
    total = 0
    while chunk := stream.read(HASH_CHUNK_BYTES):
        total += len(chunk)
        if total > max_bytes:
            raise _FileTooLarge('File exceeded the configured byte limit while reading')
        digest.update(chunk)
    return digest.hexdigest(), total


# 执行一次内容哈希，核对打开前后状态、读取字节数与路径当前指向。
def _hash_once(root: Path, candidate: FileMetadata, *, max_bytes: int,
               check_initial: bool) -> tuple[FileMetadata, str]:
    # 1. 安全打开后核对真实对象仍为授权设备上的普通文件，记录读取前状态。
    with _open_hash_source(root, candidate.relpath) as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_dev != candidate.st_dev:
            raise PermissionError('Source is no longer a regular file on the authorized device')
        before = _metadata(candidate.relpath, info)
        # 2. 首次尝试还须核对枚举快照，避免文件早已变化却直接当成原版本读取。
        if check_initial and _signature(before) != _signature(candidate):
            raise _FileChanged('File changed between enumeration and open')
        # 3. 先拒绝已知超限，再流式读取；核对读取前后状态及总字节数，任一变化丢弃摘要。
        if before.size > max_bytes:
            raise _FileTooLarge('File is larger than the configured byte limit')
        digest, total = _read_sha256(stream, max_bytes=max_bytes)
        after = _metadata(candidate.relpath, os.fstat(stream.fileno()))
        if _signature(before) != _signature(after) or total != before.size:
            raise _FileChanged('File changed during read')
        # 旧描述符可能仍指向被移走的文件；安全重开原路径，核对这个名字现在对应的对象。
        with _open_hash_source(root, candidate.relpath) as current:
            visible_info = os.fstat(current.fileno())
            if not stat.S_ISREG(visible_info.st_mode):
                raise PermissionError('Source pathname no longer points to a regular file')
            visible = _metadata(candidate.relpath, visible_info)
        if _signature(after) != _signature(visible):
            raise _FileChanged('File pathname was replaced during read')
    return after, digest


# 对单文件执行稳定读取，变化时延迟重试一次，并结构化返回失败或超限原因。
def _hash_file(root: Path, candidate: FileMetadata, *, max_bytes: int, sleep) -> FileHashResult:
    # 1. 最多尝试两次；只有内容或身份变化才延迟重试，其他错误直接生成明确失败项。
    for attempt in (1, 2):
        try:
            observation, digest = _hash_once(root, candidate, max_bytes=max_bytes,
                                              check_initial=(attempt == 1))
            return FileHashResult(**asdict(observation), sha256=digest,
                                  status='stable', attempts=attempt)
        except _FileChanged:
            # 2. 首次变化等待后重试；第二次仍变化则标 unstable，不能提供当前可用哈希。
            if attempt == 1:
                sleep(HASH_RETRY_DELAY_SECONDS)
                continue
            return FileHashResult(**asdict(candidate), sha256=None, status='unstable',
                                  attempts=attempt, error_code='changed_during_read')
        except _FileTooLarge:
            # 3. 文件超限不截断、不重试，报告 too_large 和空哈希。
            return FileHashResult(**asdict(candidate), sha256=None, status='too_large',
                                  attempts=attempt, error_code='file_too_large')
        except OSError as error:
            # 4. 把路径不安全、权限不足、来源消失和一般读取异常转为可展示的错误码。
            if error.errno in (errno.ELOOP, errno.ENOTDIR) or isinstance(error, PermissionError):
                code = 'unsafe_or_unreadable_source'
            elif isinstance(error, FileNotFoundError):
                code = 'source_not_found'
            else:
                code = 'read_failed'
            return FileHashResult(**asdict(candidate), sha256=None, status='failed',
                                  attempts=attempt, error_code=code)
        except ValueError:
            # 父目录被换成链接时，目录校验会拒绝该路径；将其作为单文件失败报告。
            return FileHashResult(**asdict(candidate), sha256=None, status='failed',
                                  attempts=attempt, error_code='unsafe_source_path')
    raise AssertionError('Unreachable retry state')


# 严格清点候选并生成稳定哈希报告，不解析正文或更新文件登记。
# 失败项元数据只是初始观察，不能当成当前版本；稳定检查也不能阻止返回后的外部修改。
def collect_file_hashes(configuration: Settings, root_id: str, *,
                        max_file_bytes: int = MAX_HASH_BYTES, sleep=time.sleep) -> HashReport:
    if type(max_file_bytes) is not int or max_file_bytes <= 0:
        raise ValueError('The file byte limit must be a positive integer')
    listing = collect_file_metadata(configuration, root_id)
    root = get_registered_root(configuration, root_id)
    results = [_hash_file(root, candidate, max_bytes=max_file_bytes, sleep=sleep)
               for candidate in listing.files]
    return HashReport(root_id=root_id, files=results, skipped=listing.skipped,
                      complete=all(item.status == 'stable' for item in results))


# 安全检查旧相对路径是否确实消失，不把链接或排除路径误认为缺失。
# 逐项核对实际名称拼写以识别大小写改名；排除路径不作为缺失证据。
def _path_is_missing(root: Path, relpath: str) -> bool:
    relative = Path(relpath)
    if relative.is_absolute() or not relative.parts or '..' in relative.parts:
        raise ValueError('Invalid stored relative path')
    if any(_excluded(Path(part)) for part in relative.parts):
        return False
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    directory_fd = os.open(root, flags)
    try:
        for index, part in enumerate(relative.parts):
            with os.scandir(directory_fd) as entries:
                present = any(entry.name == part for entry in entries)
            if not present:
                return True
            if index == len(relative.parts) - 1:
                os.stat(part, dir_fd=directory_fd, follow_symlinks=False)
                return False
            child_fd = os.open(part, flags, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = child_fd
    except FileNotFoundError:
        return True
    finally:
        os.close(directory_fd)
    return False


# 重新核对稳定文件的当前状态，并收集没有硬链接歧义的相对路径。
def _validate_hash_observations(root: Path, observations: list[FileHashResult]) -> set[str]:
    single_links = set()
    for item in observations:
        with _open_hash_source(root, item.relpath) as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or _signature(_metadata(item.relpath, info)) != _signature(item):
                raise RuntimeError('Source changed after hashing; no identity updates applied')
            if info.st_nlink == 1:
                single_links.add(item.relpath)
    return single_links


# 逐层以不跟随链接的方式打开待枚举目录，并在结束时关闭描述符。
@contextmanager
def _open_scan_directory(root: Path, relpath: str):
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in Path(relpath).parts:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


# 按目录隔离枚举故障，收集候选元数据、完整目录名单、跳过项和错误。
def _enumerate_scan_scope(configuration: Settings, root: Path):
    # 1. 检查根是否符合排除策略，初始化待遍历目录与本次观察容器。
    # 复用根目录排除规则；只取得迭代器首项，此时尚未进入子目录。
    next(iter_scan_directories(root))
    files, directories, errors = [], {}, []
    skipped = Counter()
    pending = ['']
    device = root.stat().st_dev
    while pending:
        # 2. 每个目录单独枚举；先使用局部列表，成功后再并入完整扫描观察。
        relative = pending.pop()
        local_files, children = [], []
        try:
            with _open_scan_directory(root, relative) as fd:
                if os.fstat(fd).st_dev != device:
                    raise PermissionError('Cross-device directory')
                with os.scandir(fd) as entries:
                    # 3. 完整取出名字名单；中途抛错时不会把不完整名单登记为成功范围。
                    entries = sorted(entries, key=lambda entry: entry.name)
                names = {entry.name for entry in entries}
                for entry in entries:
                    # 4. 先按名称排除，再不跟随链接读取属性；单项属性失败只记录该项错误。
                    relpath = (Path(relative) / entry.name).as_posix()
                    path = Path(entry.name)
                    if _excluded(path):
                        skipped['excluded_name'] += 1
                        continue
                    try:
                        info = entry.stat(follow_symlinks=False)
                    except OSError:
                        errors.append({'relpath': relpath, 'error_code': 'stat_failed'})
                        continue
                    if stat.S_ISLNK(info.st_mode):
                        skipped['symbolic_link'] += 1
                    elif info.st_dev != device:
                        skipped['different_device'] += 1
                    elif stat.S_ISDIR(info.st_mode):
                        children.append(relpath)
                    elif not stat.S_ISREG(info.st_mode):
                        skipped['not_regular_file'] += 1
                    elif path.suffix.lower() not in ALLOWED_EXTENSIONS:
                        skipped['extension_not_allowed'] += 1
                    else:
                        local_files.append(_metadata(relpath, info))
            # 5. 目录列举成功才保存名字证据；累计候选并检查总量，再安排符合条件的子目录。
            directories[relative] = names
            files.extend(local_files)
            if len(files) > configuration.MAX_FILES:
                raise ValueError('MAX_FILES exceeded; no file records applied')
            pending.extend(sorted(children, reverse=True))
        except OSError:
            # 即使已列出部分条目，枚举失败也不能提供缺失证据；本范围只记不可用。
            errors.append({'relpath': relative, 'error_code': 'directory_unavailable'})
            skipped['unavailable_directory'] += 1
    # 6. 返回候选、成功范围、跳过计数与错误，交给哈希和缺失判断阶段。
    return files, directories, dict(skipped), errors


# 仅利用完整成功的父目录名单，判断旧相对路径是否具有缺失证据。
def _absent_in_complete_scope(relpath: str, directories: dict[str, set[str]]) -> bool:
    parent = ''
    for part in Path(relpath).parts:
        if parent not in directories:
            return False
        if part not in directories[parent]:
            return True
        parent = (Path(parent) / part).as_posix()
    return False


# 统筹正式扫描、稳定哈希与身份协调，隔离范围故障并事务保存登记和报告。
# 独立调用默认自行取得进程锁；服务内已持锁且串行的工作线程才可关闭重复取锁。
def scan_root(configuration: Settings, root_id: str, *, max_file_bytes: int = MAX_HASH_BYTES,
              sleep=time.sleep, acquire_process_lock: bool = True, scan_id: str | None = None) -> dict:
    from app.jobs import create_scan_job, start_scan_job, fail_scan_job, save_scan_result

    # 1. 校验当前根授权；独立 CLI 自行持进程锁，服务工作线程复用服务已持有的锁。
    get_registered_root(configuration, root_id)
    guard = data_directory_lock(configuration.DATA_DIR) if acquire_process_lock else nullcontext()
    with guard:
        root = get_registered_root(configuration, root_id)
        # 2. 复用网页已创建的任务，或为独立调用新建任务，再记录 running 与开始时间。
        if scan_id is None:
            scan_id = create_scan_job(configuration.DATA_DIR, root_id)
        start_scan_job(configuration.DATA_DIR, scan_id)
        try:
            # 3. 检查单文件与总文件数限制，读取旧登记快照，供后续对账与并发校验使用。
            if type(max_file_bytes) is not int or max_file_bytes <= 0:
                raise ValueError('The file byte limit must be a positive integer')
            if type(configuration.MAX_FILES) is not int or configuration.MAX_FILES <= 0:
                raise ValueError('MAX_FILES must be a positive integer')
            previous = read_file_records(configuration.DATA_DIR, root_id)
            # 4. 按目录收集候选和成功范围，再逐文件稳定哈希；失败文件不混入稳定身份输入。
            candidates, directories, skipped, errors = _enumerate_scan_scope(configuration, root)
            observations = [_hash_file(root, item, max_bytes=max_file_bytes, sleep=sleep)
                            for item in candidates]
            stable = [item for item in observations if item.status == 'stable']
            # 5. 复核哈希后的对象状态，并找出当前只有一个硬链接名字的文件，辅助唯一改名判断。
            single_links = _validate_hash_observations(root, stable)
            observed = {item.relpath for item in observations}
            # 6. 只在成功完整枚举的范围中寻找消失的旧路径；不可访问范围不能推断删除。
            missing_paths = {old['relpath'] for old in previous if old['status'] != 'missing'
                             and old['relpath'] not in observed
                             and _absent_in_complete_scope(old['relpath'], directories)}
            # 完整目录名单和安全路径复核必须同时支持缺失判断，避免误标已重新出现的文件。
            for relpath in missing_paths:
                if not _path_is_missing(root, relpath):
                    raise RuntimeError('Source changed after enumeration')
            # 7. 对稳定文件协调新增、修改、改名和缺失身份，得到尚未写库的计划。
            plan = plan_identity_changes(root_id, previous, [asdict(item) for item in stable],
                                         confirmed_missing=missing_paths, single_link_paths=single_links)
            records = list(plan.records)
            by_path = {row['relpath']: row for row in previous if row['status'] != 'missing'}
            failures = [item for item in observations if item.status != 'stable']
            # 8. 单文件失败保留旧身份或创建失败登记，超限映射 unsupported；失败报告不给可用哈希。
            for item in failures:
                old = by_path.get(item.relpath)
                record = dict(old) if old else {
                    'id': str(uuid4()), 'root_id': root_id, 'relpath': item.relpath,
                    'dev': item.st_dev, 'ino': item.st_ino, 'size': item.size,
                    'mtime_ns': item.mtime_ns, 'ctime_ns': item.ctime_ns,
                    'sha256': None, 'content_rev': 1, 'path_rev': 1,
                    'first_seen': utc_now()}
                record['status'] = 'unsupported' if item.status == 'too_large' else item.status
                records.append(record)
                errors.append({'relpath': item.relpath, 'error_code': item.error_code})
            unavailable = []
            # 9. 旧路径既未观察到、也不能确认消失时，只标 unavailable，避免误判整个失败范围被删除。
            for old in previous:
                if old['status'] == 'missing' or old['relpath'] in observed or old['relpath'] in missing_paths:
                    continue
                record = dict(old, status='unavailable')
                records.append(record)
                unavailable.append(old['relpath'])
            # 10. 汇总正常动作、失败和跳过数量，组装同一份供持久保存与页面显示的报告。
            counts = dict(plan.counts, scanned=len(observations), stable=len(stable),
                          failed=len(failures), unavailable=len(unavailable),
                          skipped=sum(skipped.values()))
            if unavailable and not errors:
                errors.append({'relpath': '', 'error_code': 'previous_paths_not_observable'})
            report = {'mode': 'root_scan', 'scan_id': scan_id, 'root_id': root_id,
                      'verify_content': True,
                      'complete': not errors, 'applied': True, 'counts': counts,
                      'file_count': len(observations), 'skipped': skipped, 'errors': errors,
                      'files': [asdict(item) for item in plan.changes],
                      'missing': [asdict(item) for item in plan.missing],
                      'unavailable': unavailable,
                      'failed_files': [asdict(item) for item in failures],
                      'completed_directories': sorted(directories)}
            # 11. 提交前再次核对稳定来源、缺失路径与授权；若外部状态已变则整批停止。
            _validate_hash_observations(root, stable)
            for relpath in missing_paths:
                if not _path_is_missing(root, relpath):
                    raise RuntimeError('Source changed before scan commit')
            get_registered_root(configuration, root_id)
            # 12. 短事务内核验根与旧记录快照，文件更新和最终任务报告一起提交或回滚。
            with database_session(configuration.DATA_DIR) as db:
                grant = db.execute('SELECT path,dev,enabled FROM roots WHERE id=?', (root_id,)).fetchone()
                if grant is None or not grant['enabled'] or grant['path'] != str(root) or grant['dev'] != root.stat().st_dev:
                    raise RuntimeError('Root authorization changed during scan')
                apply_file_records(db, root_id, records=records,
                                   missing_ids=[item.file_id for item in plan.missing], previous=previous)
                save_scan_result(db, scan_id, report)
            return report
        except Exception as error:
            # 13. 全局异常不继续应用文件计划；保存结构化失败原因后向调用方报告错误。
            if isinstance(error, ValueError):
                code = 'max_files_exceeded' if 'MAX_FILES' in str(error) else 'invalid_scan_configuration'
            elif isinstance(error, sqlite3.Error):
                code = 'database_error'
            elif isinstance(error, OSError):
                code = 'source_unavailable'
            elif isinstance(error, RuntimeError):
                code = 'scan_state_changed'
            else:
                code = 'scan_aborted'
            fail_scan_job(configuration.DATA_DIR, scan_id, code)
            raise
