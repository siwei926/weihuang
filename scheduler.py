import datetime
import requests
from sqlalchemy import func
from apscheduler.schedulers.background import BackgroundScheduler
from config import (
    LINE_CHANNEL_ACCESS_TOKEN, LINE_USER_ID,
    PAYMENT_DUE_DAY, PAYMENT_NOTIFY_DAYS_BEFORE
)
from models import SessionLocal, LineTarget

_scheduler = BackgroundScheduler(timezone="Asia/Taipei")

PLATFORM_FOOTER = "\n\n🔗 http://192.168.1.130:8080\n⚠️ 需連接家中 WiFi 才可開啟"


def _get_line_targets() -> list[str]:
    """回傳所有已啟用的 LINE 收件對象 ID；若 DB 無資料則 fallback 至 config"""
    db = SessionLocal()
    try:
        ids = [t.target_id for t in db.query(LineTarget).filter_by(enabled=True).all()]
    finally:
        db.close()
    if not ids and LINE_USER_ID:
        ids = [LINE_USER_ID]
    return ids


def send_line_notify(message: str) -> bool:
    """透過 Line Messaging API 發送 Push Message（發給所有已啟用收件人）"""
    if not LINE_CHANNEL_ACCESS_TOKEN:
        print("[Line] Token 未設定，略過通知")
        return False
    targets = _get_line_targets()
    if not targets:
        print("[Line] 無收件對象，略過通知")
        return False
    any_ok = False
    for target_id in targets:
        try:
            resp = requests.post(
                "https://api.line.me/v2/bot/message/push",
                headers={
                    "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
                    "Content-Type": "application/json",
                },
                json={
                    "to": target_id,
                    "messages": [{"type": "text", "text": message}],
                },
                timeout=10,
            )
            if resp.status_code == 200:
                any_ok = True
            else:
                print(f"[Line] 發送至 {target_id} 失敗：{resp.status_code} {resp.text}")
        except Exception as e:
            print(f"[Line] 發送至 {target_id} 例外：{e}")
    return any_ok


def line_status() -> dict:
    """回傳 LINE 通知設定狀態，供 settings 頁面顯示"""
    return {
        "enabled": bool(LINE_CHANNEL_ACCESS_TOKEN and LINE_USER_ID),
        "has_token": bool(LINE_CHANNEL_ACCESS_TOKEN),
        "has_user_id": bool(LINE_USER_ID),
    }


# ─── 通知偏好設定（key-value 存於 AppSetting）────────────────────────────────
NOTIFY_DEFAULTS = {
    "unpaid":          {"enabled": True, "time": "09:00", "day": "*", "label": "未繳費提醒",         "fn": "check_unpaid_rentals"},
    "expiring":        {"enabled": True, "time": "09:05", "day": "*", "label": "合約到期提醒",       "fn": "check_expiring_rentals"},
    "refund":          {"enabled": True, "time": "09:10", "day": "*", "label": "待退款確認提醒",     "fn": "check_pending_refunds"},
    "auto_renew":      {"enabled": True, "time": "09:15", "day": "*", "label": "合約自動換手預告",   "fn": "check_auto_renew_upcoming"},
    "monthly_summary": {"enabled": True, "time": "08:00", "day": "1", "label": "每月收支摘要",       "fn": "check_monthly_summary"},
}

SIMPLE_NOTIFY_DEFAULTS = {
    "backup_fail": {"enabled": True, "label": "備份失敗通知"},
}


def get_setting(key: str, default: str = None) -> str:
    from models import SessionLocal, AppSetting
    db = SessionLocal()
    try:
        row = db.query(AppSetting).get(key)
        return row.value if row and row.value is not None else default
    finally:
        db.close()


def set_setting(key: str, value: str):
    from models import SessionLocal, AppSetting
    db = SessionLocal()
    try:
        row = db.query(AppSetting).get(key)
        if row:
            row.value = value
        else:
            db.add(AppSetting(key=key, value=value))
        db.commit()
    finally:
        db.close()


def get_notify_settings() -> dict:
    """取得所有時間排程通知的目前偏好設定（含預設值補齊）"""
    result = {}
    for job, defaults in NOTIFY_DEFAULTS.items():
        en = get_setting(f"notify.{job}.enabled", "true" if defaults["enabled"] else "false")
        tm = get_setting(f"notify.{job}.time", defaults["time"])
        result[job] = {
            "enabled": (en or "").lower() == "true",
            "time": tm or defaults["time"],
            "day": defaults.get("day", "*"),
            "label": defaults["label"],
        }
    return result


def save_notify_settings(form: dict):
    """form 格式：{ 'unpaid_enabled': 'on'/'', 'unpaid_time': '09:00', ... }"""
    for job in NOTIFY_DEFAULTS.keys():
        en = form.get(f"{job}_enabled") in ("on", "true", "1", True)
        set_setting(f"notify.{job}.enabled", "true" if en else "false")
        tm = (form.get(f"{job}_time") or NOTIFY_DEFAULTS[job]["time"]).strip()
        set_setting(f"notify.{job}.time", tm)
    apply_notify_settings()


def get_simple_notify_settings() -> dict:
    """取得啟用/停用型通知設定（無時間排程，由其他事件觸發）"""
    result = {}
    for job, defaults in SIMPLE_NOTIFY_DEFAULTS.items():
        en = get_setting(f"notify.{job}.enabled", "true" if defaults["enabled"] else "false")
        result[job] = {
            "enabled": (en or "").lower() == "true",
            "label": defaults["label"],
        }
    return result


def save_simple_notify_settings(form: dict):
    for job in SIMPLE_NOTIFY_DEFAULTS.keys():
        en = form.get(f"{job}_enabled") in ("on", "true", "1", True)
        set_setting(f"notify.{job}.enabled", "true" if en else "false")


def _month_end_date(d: datetime.date) -> datetime.date:
    """回傳該月最後一天"""
    if d.month == 12:
        return datetime.date(d.year, 12, 31)
    return datetime.date(d.year, d.month + 1, 1) - datetime.timedelta(days=1)


def _calc_calendar_months_total(start_date, end_date, fee=1000):
    """從 start_date 到 end_date（含）共幾個日曆月 × 月租金；最後一個月若不滿月按比例。"""
    if not start_date or not end_date or end_date < start_date:
        return 0
    months = (end_date.year - start_date.year) * 12 + (end_date.month - start_date.month) + 1
    return months * fee


def check_unpaid_rentals():
    """檢查所有出租中車位本月是否已繳清，未繳清者發 LINE 通知。
    判定方式：淨繳金額（毛繳 − 已確認退費 + 補繳豁免）是否足以涵蓋至本月底。"""
    from models import SessionLocal, Rental, Expense
    today = datetime.date.today()
    month = today.strftime("%Y-%m")
    due_day = PAYMENT_DUE_DAY

    # 當月繳費截止日
    try:
        due_date = today.replace(day=due_day)
    except ValueError:
        due_date = _month_end_date(today)
    days_to_due = (due_date - today).days

    db = SessionLocal()
    try:
        active_rentals = db.query(Rental).filter_by(status="active").all()
        unpaid = []
        for r in active_rentals:
            if r.start_date > today:
                continue  # 還沒開始的預約不算
            fee = int(r.monthly_fee or 1000)
            gross = int(sum(p.amount for p in r.payments))
            confirmed_refund = int(db.query(func.coalesce(func.sum(Expense.amount), 0))
                                     .filter(
                                         Expense.category == "退費",
                                         Expense.confirmed == True,
                                         Expense.rental_id == r.id,
                                     ).scalar() or 0)
            charge_waiver = int(db.query(func.coalesce(func.sum(Expense.amount), 0))
                                  .filter(
                                      Expense.category == "補繳豁免",
                                      Expense.rental_id == r.id,
                                  ).scalar() or 0)
            net_paid = gross - confirmed_refund + charge_waiver
            month_end = _month_end_date(today)
            expected_to_now = _calc_calendar_months_total(r.start_date, month_end, fee)
            if net_paid < expected_to_now:
                spot_num = r.spot.spot_number
                tenant_name = r.tenant.name
                plate = r.tenant.license_plate or "—"
                phone = r.tenant.phone or ""
                shortage = expected_to_now - net_paid
                contact = f"  📞{phone}" if phone else ""
                unpaid.append(f"  車位 {spot_num}｜{tenant_name}（{plate}）短繳 {shortage:,} 元{contact}")

        if unpaid:
            lines = "\n".join(unpaid)
            due_info = f"剩 {days_to_due} 天" if days_to_due >= 0 else f"已逾期 {-days_to_due} 天"
            msg = (
                f"🚨 停車場繳費提醒 — {month}\n"
                f"📅 月結截止 {due_date}（{due_info}）\n"
                f"以下車位尚未繳清：\n{lines}"
                f"{PLATFORM_FOOTER}"
            )
            send_line_notify(msg)
        return len(unpaid)
    finally:
        db.close()


def check_expiring_rentals():
    """合約即將到期提醒：30 天 / 7 天 / 1 天前各推一次"""
    from models import SessionLocal, Rental
    today = datetime.date.today()
    notify_thresholds = (30, 7, 1)
    db = SessionLocal()
    try:
        active = db.query(Rental).filter_by(status="active").all()
        groups = {n: [] for n in notify_thresholds}
        for r in active:
            if not r.expected_end_date:
                continue
            days = (r.expected_end_date - today).days
            if days in notify_thresholds:
                spot_num = r.spot.spot_number
                tenant_name = r.tenant.name
                plate = r.tenant.license_plate or "—"
                phone = r.tenant.phone or ""
                contact = f"  📞{phone}" if phone else ""
                groups[days].append(
                    f"  車位 {spot_num}｜{tenant_name}（{plate}）→ {r.expected_end_date}{contact}"
                )
        sent = 0
        for days, items in groups.items():
            if not items:
                continue
            label = "明天" if days == 1 else f"{days} 天後"
            msg = (
                f"⏰ 合約到期提醒\n"
                f"以下車位將於 {label}（{today + datetime.timedelta(days=days)}）到期：\n"
                + "\n".join(items)
                + PLATFORM_FOOTER
            )
            if send_line_notify(msg):
                sent += 1
        return sent
    finally:
        db.close()


def check_pending_refunds():
    """待退款提醒：提醒管理員確認尚未確認的退費"""
    from models import SessionLocal, Expense
    db = SessionLocal()
    try:
        pending = db.query(Expense).filter(
            Expense.category == "退費",
            Expense.confirmed == False,
        ).all()
        if not pending:
            return 0
        items = []
        total = 0
        for exp in pending:
            total += int(exp.amount)
            items.append(f"  • {exp.description}｜{int(exp.amount):,} 元")
        msg = (
            f"💰 待確認退款提醒\n"
            f"目前有 {len(pending)} 筆待確認退款（合計 {total:,} 元）：\n"
            + "\n".join(items)
            + PLATFORM_FOOTER
        )
        send_line_notify(msg)
        return len(pending)
    finally:
        db.close()


def check_auto_renew_upcoming():
    """未來 5 天內將到期的租約預告，每日早上主動通知"""
    from models import SessionLocal, Rental
    today = datetime.date.today()
    deadline = today + datetime.timedelta(days=5)
    db = SessionLocal()
    try:
        active = db.query(Rental).filter_by(status="active").all()
        items = []
        for r in active:
            if not r.expected_end_date:
                continue
            if r.expected_end_date != deadline:
                continue
            cur = r.tenant
            cur_plate = cur.license_plate or "—"
            cur_phone = f"  📞{cur.phone}" if cur.phone else ""
            current_line = f"  原車主：車位 {r.spot.spot_number}｜{cur.name}（{cur_plate}）{cur_phone}"

            # 方式一：換租（next_tenant_id 欄位）
            if r.next_tenant_id and r.next_tenant:
                nt = r.next_tenant
                nt_plate = nt.license_plate or "—"
                nt_phone = f"  📞{nt.phone}" if nt.phone else ""
                next_line = f"  新車主：{nt.name}（{nt_plate}）{nt_phone}"
            else:
                # 方式二：獨立未來租約（同車位、start_date > today、status=active）
                future = (
                    db.query(Rental)
                    .filter(
                        Rental.spot_id == r.spot_id,
                        Rental.status == "active",
                        Rental.start_date > today,
                        Rental.id != r.id,
                    )
                    .order_by(Rental.start_date)
                    .first()
                )
                if future and future.tenant:
                    nt = future.tenant
                    nt_plate = nt.license_plate or "—"
                    nt_phone = f"  📞{nt.phone}" if nt.phone else ""
                    next_line = f"  新車主：{nt.name}（{nt_plate}）{nt_phone}"
                else:
                    next_line = f"  新車主：（空置）"

            items.append(f"{current_line}\n{next_line}")
        if items:
            msg = (
                f"🔄 合約換手預告（5 天後到期）\n"
                + "\n".join(items)
                + PLATFORM_FOOTER
            )
            send_line_notify(msg)
        return len(items)
    finally:
        db.close()


def check_monthly_summary():
    """每月1號發送上月收支摘要"""
    from models import SessionLocal, Payment, Expense
    today = datetime.date.today()
    y, m = (today.year - 1, 12) if today.month == 1 else (today.year, today.month - 1)
    last_month_str = f"{y:04d}-{m:02d}"
    month_start = datetime.date(y, m, 1)
    month_end = datetime.date(y, m, 31) if m == 12 else datetime.date(y, m + 1, 1) - datetime.timedelta(days=1)

    db = SessionLocal()
    try:
        income = int(db.query(func.coalesce(func.sum(Payment.amount), 0))
                      .filter(Payment.period_month == last_month_str).scalar() or 0)
        expense = int(db.query(func.coalesce(func.sum(Expense.amount), 0))
                       .filter(Expense.confirmed == True,
                               Expense.expense_date >= month_start,
                               Expense.expense_date <= month_end).scalar() or 0)
        net = income - expense
        net_icon = "📈" if net >= 0 else "📉"
        msg = (
            f"📊 {last_month_str} 停車場收支摘要\n"
            f"💵 收入：{income:,} 元\n"
            f"💸 支出：{expense:,} 元\n"
            f"{net_icon} 淨利：{net:,} 元"
            f"{PLATFORM_FOOTER}"
        )
        send_line_notify(msg)
    finally:
        db.close()


def check_expired_rentals():
    """到達預計結束日時自動結束出租；若有預約下一位車主則自動建立新租約"""
    from models import SessionLocal, Rental, Payment
    today = datetime.date.today()
    db = SessionLocal()
    ended = []
    started = []
    try:
        active = db.query(Rental).filter_by(status="active").all()
        for r in active:
            if r.expected_end_date and r.expected_end_date <= today:
                r.status = "ended"
                r.end_date = today
                ended.append(f"  車位 {r.spot.spot_number}｜{r.tenant.name}")

                # 執行預約換租
                if r.next_tenant_id:
                    new_start = r.next_start_date or today
                    new_rental = Rental(
                        spot_id=r.spot_id,
                        tenant_id=r.next_tenant_id,
                        start_date=new_start,
                        monthly_fee=1000,
                        expected_end_date=r.next_expected_end_date,
                        original_expected_end_date=r.next_expected_end_date,
                    )
                    db.add(new_rental)
                    db.flush()
                    n_months = r.next_prepaid_months or 0
                    for i in range(n_months):
                        y = new_start.year + (new_start.month - 1 + i) // 12
                        m = (new_start.month - 1 + i) % 12 + 1
                        db.add(Payment(
                            rental_id=new_rental.id,
                            payment_date=today,
                            amount=1000,
                            period_month=f"{y:04d}-{m:02d}",
                        ))
                    nt_name = r.next_tenant.name if r.next_tenant else "—"
                    started.append(f"  車位 {r.spot.spot_number}｜{nt_name}")

        if ended:
            db.commit()
            print(f"[Scheduler] 自動結束 {len(ended)} 筆，換租 {len(started)} 筆")
    finally:
        db.close()


def auto_backup():
    try:
        from backup import create_backup
        path = create_backup()
        print(f"[Backup] 自動備份完成：{path.name}")
    except Exception as e:
        print(f"[Backup] 自動備份失敗：{e}")
        if get_setting("notify.backup_fail.enabled", "true").lower() == "true":
            send_line_notify(f"❌ 自動備份失敗！\n錯誤：{e}{PLATFORM_FOOTER}")


_NOTIFY_FN_MAP = {
    "unpaid":          check_unpaid_rentals,
    "expiring":        check_expiring_rentals,
    "refund":          check_pending_refunds,
    "auto_renew":      check_auto_renew_upcoming,
    "monthly_summary": check_monthly_summary,
}


def apply_notify_settings():
    """依目前儲存的通知偏好重新排程：依設定的時間重排，未啟用的任務移除。
    可在系統啟動時或使用者於設定頁變更後呼叫。"""
    settings = get_notify_settings()
    for job, cfg in settings.items():
        job_id = f"notify_{job}"
        # 先移除舊 job（如果存在）
        try:
            _scheduler.remove_job(job_id)
        except Exception:
            pass
        if not cfg["enabled"]:
            continue
        try:
            h, m = (cfg["time"] or "09:00").split(":")
            h, m = int(h), int(m)
        except Exception:
            h, m = 9, 0
        fn = _NOTIFY_FN_MAP.get(job)
        if not fn:
            continue
        day = NOTIFY_DEFAULTS[job].get("day", "*")
        _scheduler.add_job(
            fn,
            trigger="cron",
            day=day,
            hour=h, minute=m,
            id=job_id,
            replace_existing=True,
        )


def get_backup_interval() -> int:
    return 60  # 固定每小時整點，不開放設定


def apply_backup_settings():
    """排程自動備份：每小時 :00 整點執行"""
    try:
        _scheduler.remove_job("auto_backup")
    except Exception:
        pass
    _scheduler.add_job(
        auto_backup,
        trigger="cron",
        minute=0,
        id="auto_backup",
        replace_existing=True,
    )


def save_backup_settings(form: dict):
    apply_backup_settings()


def start_scheduler():
    if not _scheduler.running:
        _scheduler.add_job(
            check_expired_rentals,
            trigger="cron",
            hour=0, minute=5,
            id="check_expired",
            replace_existing=True,
        )
        _scheduler.start()
        # 套用使用者偏好（通知 + 備份）
        apply_notify_settings()
        apply_backup_settings()
        # 啟動時立即建一份備份
        auto_backup()
        print("[Scheduler] 已啟動")
        for job, cfg in get_notify_settings().items():
            stat = f"{cfg['time']} 啟用" if cfg["enabled"] else "已停用"
            print(f"  • {cfg['label']}：{stat}")
        print("  • 00:05 自動到期 + 換租")
        print(f"  • 每 {get_backup_interval()} 分鐘自動備份")
