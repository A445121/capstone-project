#!/usr/bin/env python3
import argparse
import datetime
import sys
from pathlib import Path


def _parse_date(value: str) -> datetime.date:
    return datetime.datetime.strptime(value, "%Y-%m-%d").date()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="每月第一天更新統計表（可搭配排程執行）"
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="忽略日期檢查，直接更新統計表",
    )
    parser.add_argument(
        "--date",
        help="指定日期 (YYYY-MM-DD)，用於測試流程",
    )
    args = parser.parse_args()

    project_root = Path(__file__).resolve().parent.parent
    sys.path.append(str(project_root))

    from src.database import db

    if args.date:
        today = _parse_date(args.date)
    else:
        today = datetime.date.today()

    if not args.force and today.day != 1:
        print(f"今天是 {today}，不是每月第一天，已略過統計更新。")
        return 0

    if db is None:
        print("資料庫尚未初始化，無法更新統計表。")
        return 1

    print("開始更新統計表...")
    db.refresh_stats()
    print("統計表更新完成。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
