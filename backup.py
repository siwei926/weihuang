import calendar
import shutil
import sqlite3
import datetime
from pathlib import Path
from config import DATA_DIR

BACKUP_DIR = Path(DATA_DIR) / "backups"
DB_PATH = Path(DATA_DIR) / "parking.db"


def create_backup() -> Path:
    """建立目前資料庫的備份，回傳備份路徑"""
    BACKUP_DIR.mkdir(exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = BACKUP_DIR / f"parking_{ts}.db"
    shutil.copy2(DB_PATH, dest)
    _cleanup_old_backups()
    return dest


def _find_closest(all_backups, target: datetime.datetime, tolerance_sec: int):
    """在 all_backups（已排序，newest first）中找最接近 target 且在容差內的備份"""
    best_f, best_ts, best_diff = None, None, None
    for f, ts in all_backups:
        diff = abs((ts - target).total_seconds())
        if diff <= tolerance_sec and (best_diff is None or diff < best_diff):
            best_f, best_ts, best_diff = f, ts, diff
    return best_f, best_ts


def _cleanup_old_backups():
    """分層備份保留策略（以整點為基準）：
    - 最近 24 小時：每小時整點（HH:00）保留 1 份
    - 最近 7 天：每天 00:00 保留 1 份
    - 最近 12 個月：每月最後一天 00:00 保留 1 份
    - 最近 10 年：每年 12/31 00:00 保留 1 份
    最多約 53 個檔案。
    """
    now = datetime.datetime.now()
    all_backups = sorted(
        [(f, _parse_ts(f)) for f in BACKUP_DIR.glob("parking_*.db")],
        key=lambda x: x[1] or datetime.datetime.min,
        reverse=True,
    )
    all_backups = [(f, ts) for f, ts in all_backups if ts is not None]

    keep = set()
    current_hour = now.replace(minute=0, second=0, microsecond=0)

    # 每小時整點，保留最近 24 小時
    for h in range(24):
        target = current_hour - datetime.timedelta(hours=h)
        if target > now:
            continue
        f, _ = _find_closest(all_backups, target, 1800)  # 30 分鐘容差
        if f:
            keep.add(f)

    # 每天 00:00，保留最近 7 天
    today_midnight = datetime.datetime.combine(now.date(), datetime.time.min)
    for d in range(7):
        target = today_midnight - datetime.timedelta(days=d)
        f, _ = _find_closest(all_backups, target, 7200)  # 2 小時容差
        if f:
            keep.add(f)

    # 每月最後一天 00:00，保留最近 12 個月
    for m in range(12):
        month = now.month - m
        year  = now.year
        while month <= 0:
            month += 12
            year  -= 1
        last_day = calendar.monthrange(year, month)[1]
        target = datetime.datetime(year, month, last_day, 0, 0, 0)
        if target > now:
            continue
        f, _ = _find_closest(all_backups, target, 86400)  # 24 小時容差
        if f:
            keep.add(f)

    # 每年 12/31 00:00，保留最近 10 年
    for y in range(10):
        year = now.year - y
        target = datetime.datetime(year, 12, 31, 0, 0, 0)
        if target > now:
            continue
        f, _ = _find_closest(all_backups, target, 86400 * 7)  # 7 天容差
        if f:
            keep.add(f)

    # 刪除不在保留清單的檔案
    for f, ts in all_backups:
        if f not in keep:
            try:
                f.unlink()
            except Exception:
                pass


def _parse_ts(path: Path) -> datetime.datetime | None:
    try:
        return datetime.datetime.strptime(path.stem.replace("parking_", ""), "%Y%m%d_%H%M%S")
    except Exception:
        return None


def list_all_backups() -> list[dict]:
    """回傳所有備份，由新到舊"""
    if not BACKUP_DIR.exists():
        return []
    files = sorted(BACKUP_DIR.glob("parking_*.db"), reverse=True)
    result = []
    for f in files:
        ts = _parse_ts(f)
        if ts:
            result.append({
                "path": f.as_posix(),
                "filename": f.name,
                "timestamp": ts.strftime("%Y-%m-%d %H:%M:%S"),
                "size_kb": round(f.stat().st_size / 1024, 1),
            })
    return result


def get_tiered_restore_points() -> dict:
    """回傳分層備份還原點，以整點為基準：
    hourly（HH:00） / daily（00:00） / monthly（月底 00:00） / yearly（12/31 00:00）
    """
    if not BACKUP_DIR.exists():
        return {"hourly": [], "daily": [], "monthly": [], "yearly": []}
    now = datetime.datetime.now()
    all_backups = sorted(
        [(f, _parse_ts(f)) for f in BACKUP_DIR.glob("parking_*.db")],
        key=lambda x: x[1] or datetime.datetime.min,
        reverse=True,
    )
    all_backups = [(f, ts) for f, ts in all_backups if ts is not None]

    def _slot(f, ts, label, sublabel):
        return {
            "path": f.as_posix(),
            "timestamp": ts.strftime("%Y-%m-%d %H:%M"),
            "label": label,
            "sublabel": sublabel,
        }

    def _empty(label, sublabel):
        return {"path": None, "timestamp": None, "label": label, "sublabel": sublabel}

    # 每小時整點（最近 24 小時）
    hourly = []
    current_hour = now.replace(minute=0, second=0, microsecond=0)
    for h in range(24):
        target = current_hour - datetime.timedelta(hours=h)
        if target > now:
            continue
        label    = target.strftime("%H:00")
        sublabel = target.strftime("%m/%d")
        f, ts = _find_closest(all_backups, target, 1800)
        hourly.append(_slot(f, ts, label, sublabel) if f else _empty(label, sublabel))

    # 每天 00:00（最近 7 天）
    daily = []
    day_labels = ["今天", "昨天", "2天前", "3天前", "4天前", "5天前", "6天前"]
    today_midnight = datetime.datetime.combine(now.date(), datetime.time.min)
    for d in range(7):
        target   = today_midnight - datetime.timedelta(days=d)
        label    = day_labels[d]
        sublabel = target.strftime("%m/%d 00:00")
        f, ts = _find_closest(all_backups, target, 7200)
        daily.append(_slot(f, ts, label, sublabel) if f else _empty(label, sublabel))

    # 每月最後一天 00:00（最近 12 個月）
    monthly = []
    for m in range(12):
        month = now.month - m
        year  = now.year
        while month <= 0:
            month += 12
            year  -= 1
        last_day = calendar.monthrange(year, month)[1]
        target   = datetime.datetime(year, month, last_day, 0, 0, 0)
        label    = f"{year}/{month:02d}"
        sublabel = f"{month:02d}/{last_day} 00:00"
        if target > now:
            monthly.append(_empty(label, sublabel))
            continue
        f, ts = _find_closest(all_backups, target, 86400)
        monthly.append(_slot(f, ts, label, sublabel) if f else _empty(label, sublabel))

    # 每年 12/31 00:00（最近 10 年）
    yearly = []
    for y in range(10):
        year     = now.year - y
        target   = datetime.datetime(year, 12, 31, 0, 0, 0)
        label    = str(year)
        sublabel = f"12/31 00:00"
        if target > now:
            yearly.append(_empty(label, sublabel))
            continue
        f, ts = _find_closest(all_backups, target, 86400 * 7)
        yearly.append(_slot(f, ts, label, sublabel) if f else _empty(label, sublabel))

    return {"hourly": hourly, "daily": daily, "monthly": monthly, "yearly": yearly}


def restore_from(backup_path: str):
    """從備份還原資料庫（會先建立當前狀態的備份）"""
    from models import engine
    create_backup()
    engine.dispose()
    src = sqlite3.connect(backup_path)
    dst = sqlite3.connect(str(DB_PATH))
    src.backup(dst)
    src.close()
    dst.close()
