"""默认正式扫描入库；显式只读模式不更新文件登记。"""

import argparse
from dataclasses import asdict
import json
import sqlite3

from app.config import load_settings
from app.process_lock import data_directory_lock
from app.scan import collect_file_hashes, collect_file_metadata, scan_root
from app.db import check_existing_database


# 解析扫描模式和本机授权，执行正式扫描或只读检查并输出 JSON 与退出码。
def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', help='Local TOML with the matching ROOT_PATH grant')
    parser.add_argument('--root-path', help='Explicit local root grant (overrides TOML)')
    parser.add_argument('--root-id', required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--verify-content', action='store_true', help='Inspect stable hashes without changing file records')
    mode.add_argument('--metadata-only', action='store_true', help='Inspect metadata without reading source bytes or updating records')
    mode.add_argument('--sync', action='store_true', help='Full scan (also the default); never change source files')
    parser.add_argument('--max-file-mib', type=int, default=20,
                        help='Per-file read limit for --verify-content/--sync (default: 20 MiB)')
    args = parser.parse_args()
    config_args = []
    for name, value in (('--config', args.config), ('--root-path', args.root_path)):
        if value is not None:
            config_args.extend([name, value])
    try:
        configuration = load_settings(config_args)
        if not args.metadata_only and not args.verify_content:
            with data_directory_lock(configuration.DATA_DIR):
                check_existing_database(configuration.DATA_DIR)
                report = scan_root(configuration, args.root_id,
                                   acquire_process_lock=False,
                                   max_file_bytes=args.max_file_mib * 1024 * 1024)
        else:
            with data_directory_lock(configuration.DATA_DIR):
                if args.verify_content:
                    report = collect_file_hashes(configuration, args.root_id,
                                                 max_file_bytes=args.max_file_mib * 1024 * 1024)
                else:
                    report = collect_file_metadata(configuration, args.root_id)
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as error:
        result_mode = 'metadata_only' if args.metadata_only else 'content_hashes' if args.verify_content else 'root_scan'
        print(json.dumps({'mode': result_mode, 'complete': False,
                          'error': str(error)}, ensure_ascii=False))
        parser.exit(1)
    result = report if isinstance(report, dict) else asdict(report)
    if not isinstance(report, dict):
        result['file_count'] = len(report.files)
    if not args.metadata_only:
        result['max_file_bytes'] = args.max_file_mib * 1024 * 1024
        if args.verify_content:
            result['stable_count'] = sum(item.status == 'stable' for item in report.files)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result['complete']:
        parser.exit(1)


if __name__ == '__main__':
    main()
