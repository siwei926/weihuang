from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, send_from_directory, abort
import datetime
import json
import os
from werkzeug.utils import secure_filename
from models import SessionLocal, init_db, ParkingSpot, Tenant, Rental, Payment, Expense, LineTarget
from sqlalchemy import func
from sqlalchemy.orm import object_session
import backup as bk
from config import SECRET_KEY, CONTRACTS_DIR
from scheduler import (
    start_scheduler, send_line_notify, line_status,
    check_unpaid_rentals, check_expiring_rentals, check_pending_refunds,
    check_auto_renew_upcoming, check_monthly_summary,
    get_notify_settings, save_notify_settings,
    get_simple_notify_settings, save_simple_notify_settings,
    get_backup_interval, save_backup_settings,
)

app = Flask(__name__)
app.secret_key = SECRET_KEY


def _last_day_of_month(y: int, m: int) -> datetime.date:
    if m == 12:
        return datetime.date(y + 1, 1, 1) - datetime.timedelta(days=1)
    return datetime.date(y, m + 1, 1) - datetime.timedelta(days=1)


def _calc_calendar_months_total(start_d: datetime.date, end_d: datetime.date, fee: int = 1000) -> int:
    """按日曆月份計費：起租月到結束月（含）各收一個月費用"""
    months = (end_d.year * 12 + end_d.month) - (start_d.year * 12 + start_d.month) + 1
    return max(0, months) * fee


def _paid_months_end_date(start: datetime.date, monthly_fee: int, total_paid: int):
    """回傳 total_paid 能覆蓋到的最後一個日曆月的最後一天（與 _calc_calendar_months_total 同邏輯）"""
    if monthly_fee <= 0 or total_paid <= 0:
        return None
    n = total_paid // monthly_fee
    if n <= 0:
        return None
    end_abs = start.year * 12 + start.month + n - 1
    ey = (end_abs - 1) // 12
    em = (end_abs - 1) % 12 + 1
    return _last_day_of_month(ey, em)


def _month_end(ym: str) -> datetime.date:
    """'YYYY-MM' → 該日曆月最後一天"""
    y, m = int(ym[:4]), int(ym[5:7])
    return _last_day_of_month(y, m)


def _count_months_after(bill_month: str, until_date: datetime.date) -> int:
    """日曆月計費：bill_month 之後到 until_date 所在月，涵蓋多少完整月份"""
    by, bm = int(bill_month[:4]), int(bill_month[5:7])
    uy, um = until_date.year, until_date.month
    return max(0, (uy * 12 + um) - (by * 12 + bm))


def _compute_prepaid_until(start_d: datetime.date, net_paid: int, fee: int = 1000):
    """以「淨繳金額 ÷ 月租金 = N 個月」從起租月推算預繳至日期（第 N 個月的月底）。
    net_paid <= 0 或 fee <= 0 時回傳 None。"""
    if net_paid <= 0 or fee <= 0:
        return None
    months = net_paid // fee
    if months <= 0:
        return None
    end_idx = start_d.month - 1 + months - 1
    y = start_d.year + end_idx // 12
    m = end_idx % 12 + 1
    return _last_day_of_month(y, m)


@app.template_filter("period_range")
def period_range_filter(ym: str, start_date=None, expected_end=None) -> str:
    """以日曆月顯示繳費期間：
    - 起租月：起租日 至 該月底
    - 中間月：該月 1 號 至 該月底
    - 結束月：該月 1 號 至 預計結束日
    """
    try:
        py, pm = int(ym[:4]), int(ym[5:7])
        first = datetime.date(py, pm, 1)
        if pm == 12:
            last = datetime.date(py, 12, 31)
        else:
            last = datetime.date(py, pm + 1, 1) - datetime.timedelta(days=1)
        cs = first
        if isinstance(start_date, datetime.date) and start_date.year == py and start_date.month == pm:
            cs = start_date
        ce = last
        if isinstance(expected_end, datetime.date) and expected_end.year == py and expected_end.month == pm:
            ce = expected_end
        if cs > ce:
            cs = ce
        return f"{cs.strftime('%Y-%m-%d')} 至 {ce.strftime('%Y-%m-%d')}"
    except Exception:
        return ym


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _db():
    db = SessionLocal()
    return db


def current_month():
    return datetime.date.today().strftime("%Y-%m")


def billing_month(start_date: datetime.date, today: datetime.date) -> str:
    """依起租日計算今天所屬的帳單月份 (YYYY-MM)。
    起租日尚在未來 → 回傳起租月份（預繳已涵蓋）。
    今日 >= 當月起租日 → 本月；否則 → 上個月。"""
    if start_date > today:
        return start_date.strftime("%Y-%m")
    if today.day >= start_date.day:
        return today.strftime("%Y-%m")
    # 上個月
    if today.month == 1:
        return f"{today.year - 1:04d}-12"
    return f"{today.year:04d}-{today.month - 1:02d}"


def spot_status(spot, today=None):
    """回傳車位狀態: disabled / vacant / paid / unpaid / warning"""
    if today is None:
        today = datetime.date.today()
    if spot.spot_type == "disabled":
        return "disabled", None
    # 找目前已開始的租約（start_date <= today）
    active = next((r for r in spot.rentals if r.status == "active" and r.start_date <= today), None)
    if not active:
        # 若只有尚未開始的預約租約，視為空置（回傳租約供儀表板提取未來預約資訊）
        futures = sorted((r for r in spot.rentals if r.status == "active" and r.start_date > today), key=lambda r: r.start_date)
        future = futures[0] if futures else None
        if future:
            return "vacant", future  # 空置，但帶著未來預約資訊
        return "vacant", None

    month = billing_month(active.start_date, today)
    # 一律以「淨繳金額是否足以涵蓋至本月底」判定，避免日期編輯後 Payment.period_month 與
    # 實際租期錯位（例：原本 5 月起租繳了 5 月份，後來改為 4 月起租，5 月份就應該是未繳）
    _tp = sum(p.amount for p in active.payments)
    _confirmed_refund = 0
    _charge_waiver = 0
    _sess = object_session(active)
    if _sess is not None:
        _confirmed_refund = int(_sess.query(
            func.coalesce(func.sum(Expense.amount), 0)
        ).filter(
            Expense.category == "退費",
            Expense.confirmed == True,
            Expense.rental_id == active.id,
        ).scalar() or 0)
        _charge_waiver = int(_sess.query(
            func.coalesce(func.sum(Expense.amount), 0)
        ).filter(
            Expense.category == "補繳豁免",
            Expense.rental_id == active.id,
        ).scalar() or 0)
    _net_tp = _tp - _confirmed_refund + _charge_waiver
    _fee = int(active.monthly_fee or 1000)
    _ce = _month_end(month)
    paid = _net_tp >= _calc_calendar_months_total(active.start_date, _ce, _fee)

    # 未繳費 → 一律紅色，不管快不快到期
    if not paid:
        return "unpaid", active

    # 已繳費 → 檢查是否快到期（預計結束日 30 天內）
    if active.expected_end_date:
        days_to_end = (active.expected_end_date - today).days
        if 0 <= days_to_end <= 30:
            return "warning", active

    # 下個帳單日在 5 天內
    next_bill_month = today.month % 12 + 1
    next_bill_year = today.year + (1 if today.month == 12 else 0)
    last_of_next = _last_day_of_month(next_bill_year, next_bill_month).day
    next_bill_day = min(active.start_date.day, last_of_next)
    next_bill_date = datetime.date(next_bill_year, next_bill_month, next_bill_day)
    if (next_bill_date - today).days <= 5:
        return "warning", active

    return "paid", active


def _merge_consecutive_same_phone(db, spot_id):
    """偵測同車位上的租約，若「電話相同（非空）」且「日期連續（next.start == prev.end + 1 天）」則合併：

    情境一：兩筆獨立 Rental 記錄相鄰
        前者吸收後者（延長 expected_end、轉移 payments、刪除後者）。

    情境二：current rental 上的「預約換租」（next_* 欄位）與本身連續
        直接延長 current.expected_end_date，清空 next_* 欄位。

    支援連續多段合併。回傳是否有變更。"""
    rentals = db.query(Rental).filter(
        Rental.spot_id == spot_id,
        Rental.status == "active",
    ).order_by(Rental.start_date).all()
    changed = False

    # 情境二：先處理「預約換租」（next_* 欄位）
    for r in rentals:
        if not r.next_tenant_id or not r.next_start_date or not r.expected_end_date:
            continue
        a_phone = (r.tenant.phone or "").strip() if r.tenant else ""
        next_tenant = db.query(Tenant).get(r.next_tenant_id)
        b_phone = (next_tenant.phone or "").strip() if next_tenant else ""
        if not a_phone or a_phone != b_phone:
            continue
        if r.next_start_date != r.expected_end_date + datetime.timedelta(days=1):
            continue
        # 合併：延長 expected_end_date 至 next_expected_end_date，清空 next_*
        r.expected_end_date = r.next_expected_end_date or r.expected_end_date
        r.next_tenant_id = None
        r.next_start_date = None
        r.next_expected_end_date = None
        r.next_prepaid_months = None
        # 清除本租約的待退費（若有）
        db.query(Expense).filter(
            Expense.category == "退費",
            Expense.confirmed == False,
            Expense.rental_id == r.id,
        ).delete(synchronize_session=False)
        db.flush()
        changed = True

    # 情境一：兩筆獨立 Rental 相鄰
    if len(rentals) >= 2:
        # 重新查詢以反映情境二可能的變更
        rentals = db.query(Rental).filter(
            Rental.spot_id == spot_id,
            Rental.status == "active",
        ).order_by(Rental.start_date).all()
        i = 0
        while i < len(rentals) - 1:
            a = rentals[i]
            b = rentals[i + 1]
            a_phone = (a.tenant.phone or "").strip() if a.tenant else ""
            b_phone = (b.tenant.phone or "").strip() if b.tenant else ""
            if not a_phone or a_phone != b_phone:
                i += 1
                continue
            if not a.expected_end_date or not b.start_date:
                i += 1
                continue
            if b.start_date != a.expected_end_date + datetime.timedelta(days=1):
                i += 1
                continue
            # 執行合併：a 吸收 b
            a.expected_end_date = b.expected_end_date
            for p in list(b.payments):
                p.rental_id = a.id
            a.refund_pending = int(a.refund_pending or 0) + int(b.refund_pending or 0)
            if b.next_tenant_id and not a.next_tenant_id:
                a.next_tenant_id = b.next_tenant_id
                a.next_start_date = b.next_start_date
                a.next_expected_end_date = b.next_expected_end_date
                a.next_prepaid_months = b.next_prepaid_months
            db.delete(b)
            db.flush()
            rentals.pop(i + 1)
            changed = True
            # 不增加 i，繼續檢查 a 是否能再合併下一筆

    if changed:
        db.commit()
    return changed


def _apply_refund(db, refund: int, refund_desc: str, today: datetime.date, full_desc: str = None, confirmed: bool = False, rental_id: int = None):
    """處理退費 Expense：refund 為本次新增的退費金額（淨額；前端已扣除既有確認退費）。
    refund_desc = 顯示前綴；full_desc = 實際顯示說明（預設同 refund_desc）。
    rental_id 用於關聯租約（未提供則僅靠描述比對舊資料）；
    confirmed = True 時直接建立已確認退費（取消租約整單作廢場景，後續無面板可確認）。
    流程：先清除既有未確認退費，再建立本次 Expense。"""
    if full_desc is None:
        full_desc = refund_desc
    # 先清除本租約的既有未確認退費，避免重複堆疊
    q = db.query(Expense).filter(
        Expense.category == "退費",
        Expense.confirmed == False,
    )
    if rental_id is not None:
        q = q.filter(Expense.rental_id == rental_id)
    else:
        q = q.filter(Expense.description.like(f"{refund_desc}%"))
    q.delete(synchronize_session=False)
    if refund > 0:
        db.add(Expense(
            expense_date=today,
            amount=refund,
            category="退費",
            description=full_desc,
            confirmed=confirmed,
            rental_id=rental_id,
        ))


# ─── 儀表板 ──────────────────────────────────────────────────────────────────

@app.route("/")
def dashboard():
    db = _db()
    try:
        today = datetime.date.today()
        # NULL sort_order 排在最後，並用 spot_number 作次要排序
        spots = db.query(ParkingSpot).order_by(
            (ParkingSpot.sort_order.is_(None)).asc(),
            ParkingSpot.sort_order.asc(),
            ParkingSpot.spot_number.asc(),
        ).all()

        # 自動合併：同電話且連續日期的租約
        for s in spots:
            _merge_consecutive_same_phone(db, s.id)
        # 合併後重新查詢以確保 in-memory 狀態同步
        for s in spots:
            db.refresh(s)

        spot_data = []
        counts = {"vacant": 0, "paid": 0, "unpaid": 0, "warning": 0, "disabled": 0, "prepaid": 0}
        for s in spots:
            status, rental = spot_status(s, today)
            # 空置但帶未來預約：把未來租約抽出，不影響帳單計算
            _all_futures = sorted(
                (r for r in s.rentals if r.status == "active" and r.start_date > today),
                key=lambda r: r.start_date,
            )
            future_rental = None
            if status == "vacant" and rental is not None:
                future_rental = rental
                rental = None
                _extra_futures = [r for r in _all_futures if r.id != future_rental.id]
            else:
                future_rental = _all_futures[0] if _all_futures else None
                _extra_futures = _all_futures[1:]
            counts[status] += 1
            bill_month = billing_month(rental.start_date, today) if rental else today.strftime("%Y-%m")
            cur_pay = next(
                (p for p in (rental.payments if rental else []) if p.period_month == bill_month),
                None
            )
            future_pays = [p for p in (rental.payments if rental else []) if p.period_month > bill_month]
            if future_pays:
                counts["prepaid"] += 1
            # 計算已繳至：包含本月繳費，不限於未來月份
            _all_pays = [p for p in (rental.payments if rental else [])]
            last_prepaid_amount = int(max(future_pays, key=lambda p: p.period_month).amount) if future_pays else 1000
            # 退費記錄（待確認 / 已確認）+ 補繳豁免 — 提前計算以供結算判斷使用
            pending_refund = None
            confirmed_refund_total = 0
            has_refund_waiver = False
            charge_waiver_total = 0
            if rental:
                pending_refund = db.query(Expense).filter(
                    Expense.category == "退費",
                    Expense.confirmed == False,
                    Expense.rental_id == rental.id,
                ).first()
                confirmed_refund_total = int(db.query(
                    func.coalesce(func.sum(Expense.amount), 0)
                ).filter(
                    Expense.category == "退費",
                    Expense.confirmed == True,
                    Expense.rental_id == rental.id,
                ).scalar())
                has_refund_waiver = db.query(Expense).filter(
                    Expense.category == "退費豁免",
                    Expense.rental_id == rental.id,
                ).count() > 0
                # 補繳豁免：房東免收的金額，視為已繳
                charge_waiver_total = int(db.query(
                    func.coalesce(func.sum(Expense.amount), 0)
                ).filter(
                    Expense.category == "補繳豁免",
                    Expense.rental_id == rental.id,
                ).scalar() or 0)
            # 預繳至：以「淨繳金額 ÷ 月租金」從起租月推算（不依賴 period_month 對齊）
            prepaid_until_date = None
            if rental and _all_pays:
                _gross_pre = int(sum(p.amount for p in _all_pays))
                _net_pre = _gross_pre - confirmed_refund_total + charge_waiver_total
                _fee_pre = int(rental.monthly_fee or 1000)
                prepaid_until_date = _compute_prepaid_until(rental.start_date, _net_pre, _fee_pre)
            # 若預繳至超過預計結束日，以結束日為上限
            if prepaid_until_date and rental and rental.expected_end_date and prepaid_until_date > rental.expected_end_date:
                prepaid_until_date = rental.expected_end_date
            prepaid_until = prepaid_until_date.strftime("%Y-%m-%d") if prepaid_until_date else ""
            # 以 prepaid_until_date 重新計算有效預繳月數（日曆月，結算法）
            effective_prepaid_months = (
                _count_months_after(bill_month, prepaid_until_date)
                if prepaid_until_date and rental
                else len(future_pays)
            )
            if not future_pays and effective_prepaid_months > 0:
                counts["prepaid"] += 1
            prepaid_expiring = (
                prepaid_until_date is not None
                and 0 <= (prepaid_until_date - today).days <= 15
            )
            nt = rental.next_tenant if rental and rental.next_tenant_id else None
            # 未來預約：補繳豁免（如有）— 提前計算供 auto-sync 使用
            future_charge_waiver_total = 0
            future_confirmed_refund_total = 0
            if future_rental:
                future_charge_waiver_total = int(db.query(
                    func.coalesce(func.sum(Expense.amount), 0)
                ).filter(
                    Expense.category == "補繳豁免",
                    Expense.rental_id == future_rental.id,
                ).scalar() or 0)
                future_confirmed_refund_total = int(db.query(
                    func.coalesce(func.sum(Expense.amount), 0)
                ).filter(
                    Expense.category == "退費",
                    Expense.confirmed == True,
                    Expense.rental_id == future_rental.id,
                ).scalar() or 0)
            # 自動同步 rental.refund_pending 與實際 gap：
            # 規則：refund_pending > 0 = 待退款；< 0 = 待補繳；= 0 = 無
            if rental and rental.expected_end_date:
                _g = int(sum(p.amount for p in rental.payments))
                _n = _g - confirmed_refund_total + charge_waiver_total
                _ex = _calc_calendar_months_total(rental.start_date, rental.expected_end_date, int(rental.monthly_fee or 1000))
                _gap = _ex - _n  # >0 應補繳；<0 應退款；=0 已繳清
                _cur_pending = int(rental.refund_pending or 0)
                if _gap > 0 and _cur_pending != -_gap:
                    rental.refund_pending = -_gap
                elif _gap < 0 and _cur_pending != -_gap and _cur_pending >= 0:
                    # 應退款；不覆寫補繳狀態
                    rental.refund_pending = -_gap   # = abs(_gap)；正值表待退款
                elif _gap == 0 and _cur_pending != 0:
                    rental.refund_pending = 0
            # 同步未來預約的 refund_pending（淨繳 = Payment 毛額 − 已確認退費 + 補繳豁免）
            # 規則：refund_pending > 0 = 待退款；< 0 = 待補繳；= 0 = 無
            if future_rental and future_rental.expected_end_date:
                _fg = int(sum(p.amount for p in future_rental.payments))
                _fn = _fg - future_confirmed_refund_total + future_charge_waiver_total
                _fex = _calc_calendar_months_total(future_rental.start_date, future_rental.expected_end_date, int(future_rental.monthly_fee or 1000))
                _fgap = _fex - _fn  # >0 應補繳；<0 應退款；=0 已繳清
                _fcur = int(future_rental.refund_pending or 0)
                if _fgap > 0 and _fcur != -_fgap and _fcur <= 0:
                    # 應補繳；只在目前不是「待退款」(>0) 時自動補上 pending（避免覆寫使用者已確認的退費狀態）
                    future_rental.refund_pending = -_fgap
                elif _fgap < 0 and _fcur != -_fgap and _fcur >= 0:
                    # 應退款；只在目前不是「待補繳」(<0) 時自動補上 pending（避免覆寫補繳狀態）
                    future_rental.refund_pending = -_fgap   # = abs(_fgap)；正值表待退款
                elif _fgap == 0 and _fcur != 0:
                    future_rental.refund_pending = 0
            db.commit()
            # 原本到期日：從所有繳費期推算最後一個月的月底（日曆月）
            original_expected_end = ""
            if rental and rental.payments:
                max_period = max(p.period_month for p in rental.payments)
                original_expected_end = _month_end(max_period).strftime("%Y-%m-%d")
            # 未來預約預繳至：以淨繳金額推算（毛繳 − 已確認退費 + 補繳豁免抵免）
            future_prepaid_until_str = ""
            if future_rental and (future_rental.payments or future_charge_waiver_total > 0):
                _f_gross = int(sum(p.amount for p in future_rental.payments))
                _f_net = _f_gross - future_confirmed_refund_total + future_charge_waiver_total
                _f_fee = int(future_rental.monthly_fee or 1000)
                _f_pu = _compute_prepaid_until(future_rental.start_date, _f_net, _f_fee)
                if _f_pu and future_rental.expected_end_date and _f_pu > future_rental.expected_end_date:
                    _f_pu = future_rental.expected_end_date
                future_prepaid_until_str = _f_pu.strftime("%Y-%m-%d") if _f_pu else ""
            spot_data.append({
                "spot": s,
                "status": status,
                "rental": rental,
                "tenant": rental.tenant if rental else None,
                "disabled_reason": s.disabled_reason or "",
                "rental_start_date": rental.start_date.strftime("%Y-%m-%d") if rental else "",
                "expected_end": rental.expected_end_date.strftime("%Y-%m-%d") if rental and rental.expected_end_date else "",
                "payment_id": cur_pay.id if cur_pay else "",
                "payment_amount": int(cur_pay.amount) if cur_pay else "",
                "prepaid_months": effective_prepaid_months,
                "last_prepaid_amount": last_prepaid_amount,
                "prepaid_until": prepaid_until,
                "prepaid_expiring": prepaid_expiring,
                "bill_month": bill_month,
                "next_tenant_id": nt.id if nt else "",
                "next_tenant_name": nt.name if nt else "",
                "next_tenant_phone": nt.phone or "" if nt else "",
                "next_tenant_plate": nt.license_plate or "" if nt else "",
                "next_start_date": rental.next_start_date.strftime("%Y-%m-%d") if rental and rental.next_start_date else "",
                "next_expected_end": rental.next_expected_end_date.strftime("%Y-%m-%d") if rental and rental.next_expected_end_date else "",
                "next_prepaid_months": rental.next_prepaid_months or 1 if rental and rental.next_tenant_id else 0,
                "rental_refund_pending": int(rental.refund_pending or 0) if rental else 0,
                "pending_refund_id": pending_refund.id if pending_refund else "",
                "pending_refund_amount": int(pending_refund.amount) if pending_refund else 0,
                "confirmed_refund_amount": confirmed_refund_total,
                "has_refund_waiver": has_refund_waiver,
                "confirmed_charge_waiver_amount": charge_waiver_total,
                "original_expected_end": original_expected_end,
                "total_paid_all": int(sum(p.amount for p in rental.payments)) if rental else 0,
                "future_rental_id": future_rental.id if future_rental else "",
                "future_rental_start": future_rental.start_date.strftime("%Y-%m-%d") if future_rental else "",
                "future_rental_expected_end": future_rental.expected_end_date.strftime("%Y-%m-%d") if future_rental and future_rental.expected_end_date else "",
                "future_tenant_id": future_rental.tenant_id if future_rental else "",
                "future_tenant_name": future_rental.tenant.name if future_rental else "",
                "future_tenant_phone": future_rental.tenant.phone or "" if future_rental else "",
                "future_tenant_plate": future_rental.tenant.license_plate or "" if future_rental else "",
                # 已預繳月數：以淨繳金額（毛繳 − 已確認退費 + 補繳豁免）÷ 月租金推算
                "future_rental_paid_months": (
                    int(
                        (int(sum(p.amount for p in future_rental.payments))
                         - future_confirmed_refund_total
                         + future_charge_waiver_total)
                        // max(1, int(future_rental.monthly_fee or 1000))
                    )
                    if future_rental else 0
                ),
                "future_rental_total_paid": int(sum(p.amount for p in future_rental.payments)) if future_rental else 0,
                "future_rental_charge_waiver_amount": future_charge_waiver_total,
                "future_rental_confirmed_refund_amount": future_confirmed_refund_total,
                "future_rental_prepaid_until": future_prepaid_until_str,
                "future_rental_refund_pending": int(future_rental.refund_pending or 0) if future_rental else 0,
                # 下任車主整體待處理狀態（給儀表板車位卡片右下角使用）：
                # 預約換租（rental.next_tenant_id 有值）尚未確認收款 → 視為「待補繳」；
                # 獨立未來租約（future_rental）依 refund_pending 正負區分。
                "future_pending_charge": bool(
                    (rental and rental.next_tenant_id)
                    or (future_rental and (future_rental.refund_pending or 0) < 0)
                ),
                "future_pending_refund": bool(
                    future_rental and (future_rental.refund_pending or 0) > 0
                ),
                "contract_count": len(_list_contracts(rental.id)) if rental else 0,
                "future_contract_count": len(_list_contracts(future_rental.id)) if future_rental else 0,
                "extra_futures_json": json.dumps([{
                    "id": r.id,
                    "start": r.start_date.strftime("%Y-%m-%d"),
                    "expected_end": r.expected_end_date.strftime("%Y-%m-%d") if r.expected_end_date else "",
                    "tenant_name": r.tenant.name,
                    "tenant_phone": r.tenant.phone or "",
                    "tenant_plate": r.tenant.license_plate or "",
                    "paid_months": len(r.payments),
                    "total_paid": int(sum(p.amount for p in r.payments)),
                    "refund_pending": int(r.refund_pending or 0),
                } for r in _extra_futures], ensure_ascii=False),
            })

        # 本月分項（會計處理：退費屬「收入扣除（銷貨退回）」，不算入支出）
        this_month = today.strftime("%Y-%m")
        month_revenue = db.query(func.coalesce(func.sum(Payment.amount), 0))\
                          .filter(func.strftime("%Y-%m", Payment.payment_date) == this_month).scalar()
        month_refund = db.query(func.coalesce(func.sum(Expense.amount), 0))\
                         .filter(
                             func.strftime("%Y-%m", Expense.expense_date) == this_month,
                             Expense.category == "退費",
                             Expense.confirmed == True,
                         ).scalar()
        month_expense = db.query(func.coalesce(func.sum(Expense.amount), 0))\
                          .filter(
                              func.strftime("%Y-%m", Expense.expense_date) == this_month,
                              ~Expense.category.in_(["退費", "退費豁免", "補繳豁免"]),
                          ).scalar()

        all_tenants = db.query(Tenant).filter_by(is_deleted=False).order_by(Tenant.name).all()

        return render_template(
            "dashboard.html",
            spot_data=spot_data,
            counts=counts,
            total=len(spots),
            month_revenue=int(month_revenue),
            month_refund=int(month_refund or 0),
            month_expense=int(month_expense),
            today=today,
            tenants=all_tenants,
            current_month=today.strftime("%Y-%m"),
        )
    finally:
        db.close()


# ─── 報表 ─────────────────────────────────────────────────────────────────────

@app.route("/reports")
def reports():
    """趨勢分析：出租率 + 年度收支對比 + 車主累計收支（依電話/Line ID 識別）"""
    db = _db()
    try:
        today = datetime.date.today()
        selected_year = int(request.args.get("year", today.year))
        # 出租率統計
        spots_for_rate = db.query(ParkingSpot).all()
        rate_counts = {"rented": 0, "vacant": 0, "disabled": 0}
        for s in spots_for_rate:
            st, _ = spot_status(s, today)
            if st in ("paid", "unpaid", "warning"):
                rate_counts["rented"] += 1
            elif st == "vacant":
                rate_counts["vacant"] += 1
            elif st == "disabled":
                rate_counts["disabled"] += 1
        months, revenues, refunds_list, expenses_list = [], [], [], []
        for mo in range(1, 13):
            m = f"{selected_year}-{mo:02d}"
            months.append(m)
            rev = db.query(func.coalesce(func.sum(Payment.amount), 0))\
                    .filter(func.strftime("%Y-%m", Payment.payment_date) == m).scalar()
            refund = db.query(func.coalesce(func.sum(Expense.amount), 0))\
                       .filter(
                           func.strftime("%Y-%m", Expense.expense_date) == m,
                           Expense.category == "退費",
                           Expense.confirmed == True,
                       ).scalar()
            exp = db.query(func.coalesce(func.sum(Expense.amount), 0))\
                    .filter(
                        func.strftime("%Y-%m", Expense.expense_date) == m,
                        ~Expense.category.in_(["退費", "退費豁免", "補繳豁免"]),
                    ).scalar()
            revenues.append(int(rev))
            refunds_list.append(int(refund or 0))
            expenses_list.append(int(exp))

        # 車主累計收支（電話/Line ID 為唯一識別 → 一位車主一列）
        tenants_summary = []
        for tenant in db.query(Tenant).filter_by(is_deleted=False).order_by(Tenant.name).all():
            rental_ids = [r.id for r in tenant.rentals]
            if not rental_ids:
                continue  # 沒有任何租約紀錄則略過
            total_paid = int(db.query(func.coalesce(func.sum(Payment.amount), 0))
                               .filter(Payment.rental_id.in_(rental_ids)).scalar() or 0)
            total_refund = int(db.query(func.coalesce(func.sum(Expense.amount), 0))
                                 .filter(
                                     Expense.category == "退費",
                                     Expense.confirmed == True,
                                     Expense.rental_id.in_(rental_ids),
                                 ).scalar() or 0)
            net_amount = total_paid - total_refund
            # 估算累計月數：以淨繳金額 ÷ 1000（標準月租）
            paid_months = net_amount // 1000 if net_amount > 0 else 0
            active_spots = sorted({
                r.spot.spot_number for r in tenant.rentals if r.status == "active"
            })
            tenants_summary.append({
                "name": tenant.name,
                "phone": tenant.phone or "—",
                "license_plate": tenant.license_plate or "—",
                "active_spots": active_spots,
                "paid_months": paid_months,
                "paid_amount": total_paid,
                "refund_amount": total_refund,
                "net_amount": net_amount,
            })
        # 依淨繳由多到少排序
        tenants_summary.sort(key=lambda x: -x["net_amount"])

        return render_template(
            "reports.html",
            months=months,
            revenues=revenues,
            refunds_chart=refunds_list,
            expenses_chart=expenses_list,
            tenants_summary=tenants_summary,
            rate_counts=rate_counts,
            selected_year=selected_year,
            current_year=today.year,
        )
    finally:
        db.close()


# ─── 車位管理 ─────────────────────────────────────────────────────────────────

@app.route("/spots")
def spots():
    db = _db()
    try:
        all_spots = db.query(ParkingSpot).order_by(ParkingSpot.spot_number).all()
        return render_template("spots.html", spots=all_spots)
    finally:
        db.close()


@app.route("/spots/add", methods=["POST"])
def spot_add():
    db = _db()
    try:
        number = request.form.get("spot_number", "").strip()
        notes = request.form.get("notes", "").strip()
        if not number:
            flash("車位編號不得為空", "danger")
            return redirect(url_for("spots"))
        if db.query(ParkingSpot).filter_by(spot_number=number).first():
            flash(f"車位 {number} 已存在", "warning")
            return redirect(url_for("spots"))
        db.add(ParkingSpot(spot_number=number, notes=notes or None))
        db.commit()
        flash(f"車位 {number} 已新增", "success")
    finally:
        db.close()
    return redirect(url_for("spots"))


@app.route("/spots/<int:spot_id>/edit", methods=["GET", "POST"])
def spot_edit(spot_id):
    db = _db()
    try:
        spot = db.query(ParkingSpot).get(spot_id)
        if not spot:
            flash("找不到該車位", "danger")
            return redirect(url_for("spots"))
        if request.method == "POST":
            spot.spot_number = request.form.get("spot_number", spot.spot_number).strip()
            spot.notes = request.form.get("notes", "").strip() or None
            new_type = request.form.get("spot_type", "rental")
            if new_type == "disabled" and any(r.status == "active" for r in spot.rentals):
                flash("請先結束出租再設為停用", "warning")
            else:
                spot.spot_type = new_type
            db.commit()
            flash("車位資訊已更新", "success")
            return redirect(url_for("spots"))
        return render_template("spot_form.html", spot=spot)
    finally:
        db.close()


@app.route("/spots/<int:spot_id>/delete", methods=["POST"])
def spot_delete(spot_id):
    db = _db()
    try:
        spot = db.query(ParkingSpot).get(spot_id)
        if not spot:
            flash("找不到該車位", "danger")
            return redirect(url_for("spots"))

        if any(r.status == "active" for r in spot.rentals):
            flash(f"車位 {spot.spot_number} 出租中，請先結束出租再刪除", "warning")
            return redirect(url_for("spots"))

        db.delete(spot)
        db.commit()
        flash(f"車位 {spot.spot_number} 已刪除", "success")
    finally:
        db.close()
    return redirect(url_for("spots"))


# ─── 車主管理 ─────────────────────────────────────────────────────────────────

@app.route("/tenants")
def tenants():
    db = _db()
    try:
        all_tenants = db.query(Tenant).filter_by(is_deleted=False).order_by(Tenant.name).all()
        return render_template("tenants.html", tenants=all_tenants)
    finally:
        db.close()


@app.route("/tenants/add", methods=["POST"])
def tenant_add():
    db = _db()
    try:
        name  = request.form.get("name", "").strip()
        phone = request.form.get("phone", "").strip() or None
        plate = request.form.get("license_plate", "").strip() or None
        notes = request.form.get("notes", "").strip() or None

        if not name:
            flash("姓名不得為空", "danger")
            return redirect(url_for("tenants"))
        if not phone:
            flash("電話/Line ID 為必填（作為車主唯一身份識別）", "danger")
            return redirect(url_for("tenants"))

        # 電話作為車主身份識別，必須唯一（排除軟刪除車主）
        existing = db.query(Tenant).filter(
            Tenant.phone == phone,
            Tenant.is_deleted == False,
        ).first()
        if existing:
            flash(f"電話/Line ID「{phone}」已被「{existing.name}」使用，請確認後再新增", "warning")
            return redirect(url_for("tenants"))

        db.add(Tenant(name=name, phone=phone, license_plate=plate, notes=notes))
        db.commit()
        flash(f"「{name}」已新增", "success")
    finally:
        db.close()
    return redirect(url_for("tenants"))


@app.route("/tenants/<int:tenant_id>/edit", methods=["GET", "POST"])
def tenant_edit(tenant_id):
    db = _db()
    try:
        tenant = db.query(Tenant).get(tenant_id)
        if not tenant:
            flash("找不到該車主", "danger")
            return redirect(url_for("tenants"))
        if request.method == "POST":
            new_name  = request.form.get("name", tenant.name).strip()
            new_phone = request.form.get("phone", "").strip() or None
            new_plate = request.form.get("license_plate", "").strip() or None
            new_notes = request.form.get("notes", "").strip() or None
            if not new_name:
                flash("姓名不得為空", "danger")
                return render_template("tenant_form.html", tenant=tenant)
            if not new_phone:
                flash("電話/Line ID 為必填（作為車主唯一身份識別）", "danger")
                return render_template("tenant_form.html", tenant=tenant)
            # 電話作為身份識別：若電話有變更，需檢查不可與他人重複
            if new_phone and new_phone != tenant.phone:
                existing = db.query(Tenant).filter(
                    Tenant.phone == new_phone,
                    Tenant.is_deleted == False,
                    Tenant.id != tenant.id,
                ).first()
                if existing:
                    flash(f"電話/Line ID「{new_phone}」已被「{existing.name}」使用，請改用其他", "warning")
                    return render_template("tenant_form.html", tenant=tenant)
            tenant.name = new_name
            tenant.phone = new_phone
            tenant.license_plate = new_plate
            tenant.notes = new_notes
            db.commit()
            flash("車主資訊已更新", "success")
            return redirect(url_for("tenants"))
        return render_template("tenant_form.html", tenant=tenant)
    finally:
        db.close()


@app.route("/tenants/<int:tenant_id>/delete", methods=["POST"])
def tenant_delete(tenant_id):
    db = _db()
    try:
        tenant = db.query(Tenant).get(tenant_id)
        if not tenant:
            flash("找不到該車主", "danger")
            return redirect(url_for("tenants"))

        # 有出租中 → 擋住
        active = [r for r in tenant.rentals if r.status == "active"]
        if active:
            spots = "、".join(r.spot.spot_number for r in active)
            flash(f"無法刪除「{tenant.name}」：車位 {spots} 仍出租中，請先結束出租", "warning")
            return redirect(url_for("tenants"))

        # 有歷史記錄 → 軟刪除（保留出租/繳費紀錄）
        if tenant.rentals:
            tenant.is_deleted = True
            db.commit()
            flash(f"「{tenant.name}」已從車主列表移除，歷史出租與繳費紀錄已保留", "success")
        else:
            # 完全沒有記錄 → 直接刪除
            db.delete(tenant)
            db.commit()
            flash(f"「{tenant.name}」已刪除", "success")
    finally:
        db.close()
    return redirect(url_for("tenants"))


# ─── 出租管理 ─────────────────────────────────────────────────────────────────

@app.route("/rentals")
def rentals():
    db = _db()
    try:
        all_rentals = db.query(Rental).order_by(Rental.status, Rental.start_date.desc()).all()
        free_spots = db.query(ParkingSpot).filter_by(is_active=True).all()
        free_spots = [
            s for s in free_spots
            if not any(r.status == "active" for r in s.rentals)
        ]
        all_tenants = db.query(Tenant).filter_by(is_deleted=False).order_by(Tenant.name).all()
        contracts = {r.id: _list_contracts(r.id) for r in all_rentals}
        contract_totals = {
            r.id: _calc_calendar_months_total(
                r.start_date, r.expected_end_date, int(r.monthly_fee or 1000)
            ) if r.expected_end_date else None
            for r in all_rentals
        }
        # 計算每筆租約的退款/補款調整（用於備註欄）
        contract_adjustments = {}
        for r in all_rentals:
            orig = contract_totals.get(r.id)
            if not orig:
                continue
            confirmed_refund = int(db.query(func.coalesce(func.sum(Expense.amount), 0)).filter(
                Expense.rental_id == r.id,
                Expense.category == "退費",
                Expense.confirmed == True,
            ).scalar() or 0)
            pending_refund = int(db.query(func.coalesce(func.sum(Expense.amount), 0)).filter(
                Expense.rental_id == r.id,
                Expense.category == "退費",
                Expense.confirmed == False,
            ).scalar() or 0)
            total_refund = confirmed_refund + pending_refund
            total_paid = int(sum(p.amount for p in r.payments))
            fee = int(r.monthly_fee or 1000)
            if total_refund > 0:
                # 有退款（含待退款）：已繳總額－退款＝實收
                # orig_end = 原始付款涵蓋月份（total_paid 算出），new_end = 退款後更新的合約結束日
                contract_adjustments[r.id] = {
                    "type": "refund",
                    "original": total_paid,
                    "delta": total_refund,
                    "net": total_paid - total_refund,
                    "start": r.start_date,
                    "orig_end": _paid_months_end_date(r.start_date, fee, total_paid),
                    "new_end": r.expected_end_date,
                    "pending": pending_refund > 0 and confirmed_refund == 0,
                }
            elif total_paid > orig:
                # 補款（多付）：合約原結束日＋多付月份到新結束日
                contract_adjustments[r.id] = {
                    "type": "supplement",
                    "original": orig,
                    "delta": total_paid - orig,
                    "net": total_paid,
                    "start": r.start_date,
                    "orig_end": r.expected_end_date,
                    "new_end": _paid_months_end_date(r.start_date, fee, total_paid),
                }
            elif total_paid > 0 and total_paid < orig:
                # 延長後尚未補繳：已繳涵蓋至原結束日，新結束日為延長後合約
                contract_adjustments[r.id] = {
                    "type": "pending",
                    "original": total_paid,
                    "delta": orig - total_paid,
                    "net": orig,
                    "start": r.start_date,
                    "orig_end": _paid_months_end_date(r.start_date, fee, total_paid),
                    "new_end": r.expected_end_date,
                }
            elif total_paid == orig and total_paid > 0:
                orig_end = r.original_expected_end_date
                cur_end  = r.expected_end_date
                if orig_end and cur_end and orig_end != cur_end:
                    # 延長合約且已全額繳清
                    orig_contract = _calc_calendar_months_total(r.start_date, orig_end, fee)
                    contract_adjustments[r.id] = {
                        "type": "extended_paid",
                        "original": orig_contract,
                        "delta": orig - orig_contract,
                        "net": orig,
                        "start": r.start_date,
                        "orig_end": orig_end,
                        "new_end": cur_end,
                    }
                else:
                    # 正常已繳清：無異動
                    contract_adjustments[r.id] = {
                        "type": "paid",
                        "original": orig,
                        "start": r.start_date,
                        "orig_end": cur_end,
                    }
        return render_template("rentals.html",
                               rentals=all_rentals,
                               free_spots=free_spots,
                               tenants=all_tenants,
                               contracts=contracts,
                               contract_totals=contract_totals,
                               contract_adjustments=contract_adjustments,
                               today=datetime.date.today())
    finally:
        db.close()


@app.route("/rentals/add", methods=["POST"])
def rental_add():
    db = _db()
    try:
        spot_id = request.form.get("spot_id")
        tenant_id = request.form.get("tenant_id")
        start_date = request.form.get("start_date") or str(datetime.date.today())
        notes = request.form.get("notes", "").strip()

        if not spot_id or not tenant_id:
            flash("請選擇車位與車主", "danger")
            return redirect(url_for("rentals"))

        spot = db.query(ParkingSpot).get(int(spot_id))
        if any(r.status == "active" for r in spot.rentals):
            flash(f"車位 {spot.spot_number} 已有出租中記錄", "warning")
            return redirect(url_for("rentals"))

        db.add(Rental(
            spot_id=int(spot_id),
            tenant_id=int(tenant_id),
            start_date=datetime.date.fromisoformat(start_date),
            monthly_fee=1000,
            notes=notes or None,
        ))
        db.commit()
        flash("出租記錄已新增", "success")
    finally:
        db.close()
    return redirect(url_for("rentals"))


@app.route("/rentals/<int:rental_id>/end", methods=["POST"])
def rental_end(rental_id):
    db = _db()
    try:
        rental = db.query(Rental).get(rental_id)
        if rental:
            rental.status = "ended"
            rental.end_date = datetime.date.today()
            db.commit()
            flash("出租已結束", "success")
    finally:
        db.close()
    return redirect(url_for("rentals"))


_ALLOWED_CONTRACT_EXT = {'jpg', 'jpeg', 'png', 'pdf', 'heic', 'heif'}

def _contract_dir(rental_id):
    d = os.path.join(CONTRACTS_DIR, str(rental_id))
    os.makedirs(d, exist_ok=True)
    return d

def _list_contracts(rental_id):
    d = os.path.join(CONTRACTS_DIR, str(rental_id))
    if not os.path.isdir(d):
        return []
    files = sorted(os.listdir(d))
    return files


@app.route("/api/contract-count/<int:rental_id>")
def api_contract_count(rental_id):
    return jsonify({"count": len(_list_contracts(rental_id))})


@app.route("/rentals/<int:rental_id>/contract")
def rental_contract_page(rental_id):
    db = _db()
    try:
        rental = db.query(Rental).get(rental_id)
        if not rental:
            abort(404)
        files = _list_contracts(rental_id)
        upload_url = url_for('rental_contract_upload', rental_id=rental_id)
        delete_url = url_for('rental_contract_delete', rental_id=rental_id)

        # flash messages
        from flask import get_flashed_messages
        flash_html = ""
        for cat, msg in get_flashed_messages(with_categories=True):
            bg = "#d1e7dd" if cat == "success" else "#f8d7da"
            tc = "#0a3622" if cat == "success" else "#58151c"
            flash_html += f'<div style="background:{bg};color:{tc};padding:10px 16px;border-radius:10px;margin:12px 16px 0;font-size:.88rem">{msg}</div>'

        # files grid
        files_section = ""
        if files:
            cards = ""
            for fname in files:
                ext = fname.rsplit('.', 1)[-1].lower()
                file_url = url_for('rental_contract_file', rental_id=rental_id, filename=fname)
                if ext in ('jpg','jpeg','png','heic','heif'):
                    preview = (f'<a href="{file_url}" target="_blank">'
                               f'<img src="{file_url}" style="max-width:100%;max-height:140px;border-radius:8px;object-fit:cover"></a>')
                else:
                    preview = (f'<a href="{file_url}" target="_blank" style="text-decoration:none">'
                               f'<div style="font-size:2.4rem">📄</div>'
                               f'<div style="font-size:.7rem;color:#666;word-break:break-all;margin-top:4px">{fname}</div></a>')
                cards += (
                    f'<div style="background:#fff;border-radius:12px;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,.09)">'
                    f'<div style="padding:12px;text-align:center;min-height:100px;display:flex;align-items:center;justify-content:center;flex-direction:column">{preview}</div>'
                    f'<form method="post" action="{delete_url}" onsubmit="return confirm(\'確定刪除？\')">'
                    f'<input type="hidden" name="filename" value="{fname}">'
                    f'<button style="display:block;width:100%;padding:9px;background:none;color:#dc3545;border:none;border-top:1px solid #f2f2f2;font-size:.82rem;cursor:pointer">🗑 刪除</button>'
                    f'</form></div>'
                )
            files_section = (
                f'<div style="font-size:.8rem;font-weight:600;color:#888;letter-spacing:.05em;margin:4px 16px 10px">已上傳 {len(files)} 個檔案</div>'
                f'<div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;margin:0 16px 20px">{cards}</div>'
            )
        else:
            files_section = '<div style="text-align:center;color:#ccc;padding:24px;font-size:.9rem">尚未上傳任何合約照片</div>'

        html = f"""<!DOCTYPE html><html><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,"Helvetica Neue",sans-serif;background:#f5f6fa;color:#333;font-size:15px}}
input[type=file]{{display:block;width:100%;font-size:.95rem;touch-action:manipulation;margin-bottom:14px}}
</style>
</head><body>
{flash_html}
<div style="background:#fff;border-radius:14px;margin:16px;padding:20px;box-shadow:0 2px 10px rgba(0,0,0,.07)">
  <div style="font-weight:600;font-size:.9rem;color:#444;margin-bottom:10px">📷 選擇照片 / 拍照 / PDF</div>
  <form method="post" action="{upload_url}" enctype="multipart/form-data">
    <input type="file" name="file" accept="image/*,.pdf">
    <div style="font-size:.78rem;color:#aaa;margin-bottom:16px">支援 JPG、PNG、PDF、HEIC</div>
    <button type="submit" style="display:block;width:100%;padding:14px;background:#0d6efd;color:#fff;border:none;border-radius:10px;font-size:1rem;font-weight:600;cursor:pointer;touch-action:manipulation">上傳</button>
  </form>
</div>
{files_section}
</body></html>"""
        resp = app.make_response(html)
        resp.headers['Cache-Control'] = 'no-store'
        return resp
    finally:
        db.close()


@app.route("/rentals/<int:rental_id>/contract/upload", methods=["POST"])
def rental_contract_upload(rental_id):
    f = request.files.get("file")
    if not f or f.filename == "":
        flash("未選擇檔案", "danger")
        return redirect(url_for("rental_contract_page", rental_id=rental_id))
    ext = f.filename.rsplit(".", 1)[-1].lower() if "." in f.filename else ""
    if ext not in _ALLOWED_CONTRACT_EXT:
        flash("不支援的格式（支援：JPG、PNG、PDF、HEIC）", "danger")
        return redirect(url_for("rental_contract_page", rental_id=rental_id))
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{ts}_{secure_filename(f.filename)}"
    f.save(os.path.join(_contract_dir(rental_id), filename))
    flash("合約照片已上傳", "success")
    if request.form.get("from") == "modal":
        return redirect(url_for("rentals"))
    return redirect(url_for("rental_contract_page", rental_id=rental_id))


@app.route("/rentals/<int:rental_id>/contract/file/<path:filename>")
def rental_contract_file(rental_id, filename):
    return send_from_directory(_contract_dir(rental_id), filename)


@app.route("/rentals/<int:rental_id>/contract/delete", methods=["POST"])
def rental_contract_delete(rental_id):
    filename = request.form.get("filename", "")
    path = os.path.join(_contract_dir(rental_id), secure_filename(filename))
    if os.path.isfile(path):
        os.remove(path)
        flash("已刪除合約照片", "success")
    return redirect(url_for("rental_contract_page", rental_id=rental_id))


@app.route("/rentals/<int:rental_id>/delete", methods=["POST"])
def rental_delete(rental_id):
    db = _db()
    try:
        rental = db.query(Rental).get(rental_id)
        if rental:
            db.delete(rental)
            db.commit()
            flash("出租記錄已刪除", "success")
    finally:
        db.close()
    return redirect(url_for("rentals"))


# ─── 繳費管理 ─────────────────────────────────────────────────────────────────

@app.route("/payments")
def payments():
    db = _db()
    try:
        month_filter = request.args.get("month", current_month())
        raw = (
            db.query(Payment)
            .filter(func.strftime("%Y-%m", Payment.payment_date) == month_filter)
            .order_by(Payment.payment_date.asc(), Payment.rental_id.asc(), Payment.period_month.asc())
            .all()
        )

        # 依 (spot_id, tenant_id, payment_date) 分組，合併預繳多筆；
        # 同車位同車主同日的繳費會合併為一列（即使屬於不同 Rental 紀錄，例如歷經移位/重建）
        from collections import defaultdict
        groups = defaultdict(list)
        for p in raw:
            groups[(p.rental.spot_id, p.rental.tenant_id, p.payment_date)].append(p)

        payment_rows = []
        total_amount = 0
        group_counter = 0
        # 排序：依繳費日期 → 車位 → 車主
        for ps in [v for _, v in sorted(groups.items(), key=lambda x: (x[0][2], x[0][0], x[0][1]))]:
            ps_sorted = sorted(ps, key=lambda p: p.period_month)
            amt = sum(p.amount for p in ps_sorted)
            total_amount += int(amt)
            if len(ps_sorted) == 1:
                payment_rows.append({"group": False, "p": ps_sorted[0]})
            else:
                group_counter += 1
                # 日曆月：第一筆 = 起租月則用起租日，否則用月初；最後一筆 = 結束月則用結束日，否則用月底
                _rstart = ps_sorted[0].rental.start_date
                _rend   = ps_sorted[0].rental.expected_end_date
                _first_pm = ps_sorted[0].period_month
                _fy, _fm = int(_first_pm[:4]), int(_first_pm[5:7])
                if _rstart.year == _fy and _rstart.month == _fm:
                    first_cs = _rstart
                else:
                    first_cs = datetime.date(_fy, _fm, 1)
                _last_pm = ps_sorted[-1].period_month
                last_ce = _month_end(_last_pm)
                if _rend and last_ce > _rend:
                    last_ce = _rend
                payment_rows.append({
                    "group": True,
                    "group_id": group_counter,
                    "rental": ps_sorted[0].rental,
                    "payment_date": ps_sorted[0].payment_date,
                    "period_str": f"{first_cs.strftime('%Y-%m-%d')} 至 {last_ce.strftime('%Y-%m-%d')}",
                    "amount": int(amt),
                    "count": len(ps_sorted),
                    "ids": ",".join(str(p.id) for p in ps_sorted),
                    "notes": ps_sorted[0].notes or "—",
                    "details": ps_sorted,
                })

        active_rentals = db.query(Rental).filter_by(status="active").all()
        return render_template("payments.html",
                               payment_rows=payment_rows,
                               total_amount=total_amount,
                               active_rentals=active_rentals,
                               month_filter=month_filter,
                               today=datetime.date.today())
    finally:
        db.close()


@app.route("/payments/add", methods=["POST"])
def payment_add():
    db = _db()
    try:
        rental_id = request.form.get("rental_id")
        payment_date = request.form.get("payment_date") or str(datetime.date.today())
        amount = request.form.get("amount", "0")
        period_month = request.form.get("period_month", current_month())
        notes = request.form.get("notes", "").strip()

        if not rental_id:
            flash("請選擇出租記錄", "danger")
            return redirect(url_for("payments"))

        rental = db.query(Rental).get(int(rental_id))
        if not rental:
            flash("找不到出租記錄", "danger")
            return redirect(url_for("payments"))

        amt = int(amount)
        pay_d = datetime.date.fromisoformat(payment_date)
        fee   = int(rental.monthly_fee or 1000)

        # 自動拆分：若金額為月租金的整數倍且 ≥ 2 個月，自動建立 N 筆月繳紀錄
        # 從指定的 period_month 開始往後排，跳過已存在的月份
        months = amt // fee if fee > 0 else 0
        if fee > 0 and amt > 0 and amt % fee == 0 and months >= 2:
            base_y, base_m = int(period_month[:4]), int(period_month[5:7])
            existing = {p.period_month for p in rental.payments}
            created = 0
            i = 0
            while created < months and i < months * 4:  # 防呆上限
                y = base_y + (base_m - 1 + i) // 12
                m = (base_m - 1 + i) % 12 + 1
                pm = f"{y:04d}-{m:02d}"
                i += 1
                if pm in existing:
                    continue
                db.add(Payment(
                    rental_id=rental.id,
                    payment_date=pay_d,
                    amount=fee,
                    period_month=pm,
                    notes=notes or None,
                ))
                existing.add(pm)
                created += 1
            db.commit()
            flash(f"繳費記錄已新增（自動拆分為 {created} 個月）", "success")
        else:
            db.add(Payment(
                rental_id=rental.id,
                payment_date=pay_d,
                amount=amt,
                period_month=period_month,
                notes=notes or None,
            ))
            db.commit()
            flash("繳費記錄已新增", "success")
    finally:
        db.close()
    return redirect(url_for("payments"))


@app.route("/payments/<int:payment_id>/delete", methods=["POST"])
def payment_delete(payment_id):
    db = _db()
    try:
        pay = db.query(Payment).get(payment_id)
        if pay:
            db.delete(pay)
            db.commit()
            flash("繳費記錄已刪除", "success")
    finally:
        db.close()
    return redirect(url_for("payments"))


@app.route("/payments/group-delete", methods=["POST"])
def payment_group_delete():
    ids_str = request.form.get("ids", "")
    ids = [int(x) for x in ids_str.split(",") if x.strip().isdigit()]
    db = _db()
    try:
        for pid in ids:
            pay = db.query(Payment).get(pid)
            if pay:
                db.delete(pay)
        db.commit()
        flash(f"已刪除 {len(ids)} 筆繳費記錄", "success")
    finally:
        db.close()
    return redirect(request.referrer or url_for("payments"))


# ─── 儀表板快速操作 API ───────────────────────────────────────────────────────

@app.route("/api/quick-tenant", methods=["POST"])
def quick_tenant():
    """從儀表板快速建立車主，回傳新車主 id"""
    db = _db()
    try:
        data = request.get_json()
        name  = (data.get("name") or "").strip()
        phone = (data.get("phone") or "").strip() or None
        plate = (data.get("license_plate") or "").strip() or None
        if not name:
            return jsonify({"ok": False, "msg": "姓名不得為空"})
        if not phone:
            return jsonify({"ok": False, "msg": "電話/Line ID 為必填（作為車主唯一身份識別）"})
        # 電話作為車主身份識別，必須唯一（排除軟刪除車主）
        existing = db.query(Tenant).filter(
            Tenant.phone == phone,
            Tenant.is_deleted == False,
        ).first()
        if existing:
            return jsonify({"ok": False, "msg": f"電話/Line ID「{phone}」已被「{existing.name}」使用"})
        t = Tenant(name=name, phone=phone, license_plate=plate)
        db.add(t)
        db.commit()
        return jsonify({"ok": True, "tenant_id": t.id})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})
    finally:
        db.close()


@app.route("/api/quick-rent", methods=["POST"])
def quick_rent():
    """從儀表板快速建立出租"""
    db = _db()
    try:
        data = request.get_json()
        spot_id = int(data.get("spot_id", 0))
        tenant_id = int(data.get("tenant_id", 0))
        start_date = data.get("start_date") or str(datetime.date.today())

        spot = db.query(ParkingSpot).get(spot_id)
        if not spot:
            return jsonify({"ok": False, "msg": "找不到車位"})

        expected_end = data.get("expected_end_date") or None
        start_d = datetime.date.fromisoformat(start_date)
        today_d = datetime.date.today()

        for r in spot.rentals:
            if r.status != "active":
                continue
            if r.start_date <= today_d:
                return jsonify({"ok": False, "msg": f"車位 {spot.spot_number} 已有出租中記錄"})
            # 存在未來預約租約：新租約結束日必須早於預約起租日
            if expected_end:
                end_d = datetime.date.fromisoformat(expected_end)
                if end_d >= r.start_date:
                    return jsonify({"ok": False, "msg": f"結束日不可晚於或等於預約起租日 {r.start_date}，請調整結束日"})
        _end_d = datetime.date.fromisoformat(expected_end) if expected_end else None
        rental = Rental(
            spot_id=spot_id,
            tenant_id=tenant_id,
            start_date=start_d,
            monthly_fee=1000,
            expected_end_date=_end_d,
            original_expected_end_date=_end_d,
        )
        db.add(rental)
        db.flush()  # 取得 rental.id

        # 出租建立時一律不預記繳費，須事後用確認收款按鈕
        db.commit()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})
    finally:
        db.close()


@app.route("/api/quick-pay", methods=["POST"])
def quick_pay():
    """從儀表板快速新增本月繳費"""
    db = _db()
    try:
        data = request.get_json()
        rental_id = int(data.get("rental_id", 0))
        amount = int(data.get("amount", 0))
        period_month = data.get("period_month", current_month())

        rental = db.query(Rental).get(rental_id)
        if not rental:
            return jsonify({"ok": False, "msg": "找不到出租記錄"})
        if any(p.period_month == period_month for p in rental.payments):
            return jsonify({"ok": False, "msg": f"{period_month} 已有繳費記錄"})

        db.add(Payment(
            rental_id=rental_id,
            payment_date=datetime.date.today(),
            amount=amount,
            period_month=period_month,
        ))
        db.commit()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})
    finally:
        db.close()


@app.route("/api/add-prepaid", methods=["POST"])
def add_prepaid():
    """從最後一筆已繳月份起，追加建立預繳記錄"""
    db = _db()
    try:
        data = request.get_json()
        rental_id = int(data.get("rental_id", 0))
        months = max(0, int(data.get("months", 0)))
        # 末月可能按比例收費（來自 calcFullPeriod 的 lastAmount）
        last_amount = int(data.get("last_amount", 0)) or None
        rental = db.query(Rental).get(rental_id)
        if not rental:
            return jsonify({"ok": False, "msg": "找不到出租記錄"})
        if months == 0:
            return jsonify({"ok": True, "msg": "已繳清，無需建立繳費紀錄"})

        existing = {p.period_month for p in rental.payments}
        # 結算模式：由前端指定起始 period，用於已繳總額不等於 period 筆數 × 月租的場景
        start_period = data.get("start_period")  # e.g. "2027-01"
        if start_period:
            base_year, base_month = int(start_period[:4]), int(start_period[5:])
            base_month -= 1  # 迴圈第一次 +1 後會得到指定月份
        elif rental.start_date:
            # 從起租月開始迭代，遇已存在的 period 跳過 → 自動填補缺漏（含修改起租日後的場景）
            base_year, base_month = rental.start_date.year, rental.start_date.month
            base_month -= 1
        else:
            base_year, base_month = datetime.date.today().year, datetime.date.today().month
            base_month -= 1

        today = datetime.date.today()
        last_period = ""
        created = 0
        i = 0
        max_iter = months * 12 + 36  # 防呆上限
        while created < months and i < max_iter:
            i += 1
            y = base_year + (base_month - 1 + i) // 12
            m = (base_month - 1 + i) % 12 + 1
            period = f"{y:04d}-{m:02d}"
            if period in existing:
                continue
            is_last = (created + 1 == months)
            amount = (last_amount if (is_last and last_amount) else int(rental.monthly_fee))
            db.add(Payment(
                rental_id=rental_id,
                payment_date=today,
                amount=amount,
                period_month=period,
            ))
            existing.add(period)
            last_period = period
            created += 1

        # 同步更新預計結束日 = 最後一個預繳月份的月底（日曆月，僅在未設定時）
        if last_period:
            new_end = _month_end(last_period)
            if rental.expected_end_date is None:
                rental.expected_end_date = new_end
                if rental.original_expected_end_date is None:
                    rental.original_expected_end_date = new_end

        # 確認補繳後清除待補繳/待退款旗標
        rental.refund_pending = 0

        db.commit()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})
    finally:
        db.close()


@app.route("/api/update-payment", methods=["POST"])
def update_payment():
    """修改繳費金額"""
    db = _db()
    try:
        data = request.get_json()
        pid = data.get("payment_id")
        if not pid:
            return jsonify({"ok": False, "msg": "找不到繳費記錄"})
        pay = db.query(Payment).get(int(pid))
        if not pay:
            return jsonify({"ok": False, "msg": "找不到繳費記錄"})
        pay.amount = int(data.get("amount", pay.amount))
        db.commit()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})
    finally:
        db.close()


@app.route("/api/delete-payment", methods=["POST"])
def delete_payment_api():
    """從儀表板刪除繳費記錄"""
    db = _db()
    try:
        data = request.get_json()
        pid = data.get("payment_id")
        if not pid:
            return jsonify({"ok": False, "msg": "找不到繳費記錄"})
        pay = db.query(Payment).get(int(pid))
        if not pay:
            return jsonify({"ok": False, "msg": "找不到繳費記錄"})
        db.delete(pay)
        db.commit()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})
    finally:
        db.close()


@app.route("/api/update-rental", methods=["POST"])
def update_rental():
    """從儀表板更新租約資訊（車主、租金、起租日、預計結束日）"""
    db = _db()
    try:
        data = request.get_json()
        rental_id = int(data.get("rental_id", 0))
        rental = db.query(Rental).get(rental_id)
        if not rental:
            return jsonify({"ok": False, "msg": "找不到出租記錄"})

        tenant_id = data.get("tenant_id")
        if tenant_id:
            rental.tenant_id = int(tenant_id)

        monthly_fee = data.get("monthly_fee")
        if monthly_fee is not None:
            rental.monthly_fee = int(monthly_fee)

        start_date = data.get("start_date")
        if start_date:
            rental.start_date = datetime.date.fromisoformat(start_date)

        expected_end = data.get("expected_end_date")
        rental.expected_end_date = (
            datetime.date.fromisoformat(expected_end) if expected_end else None
        )
        if rental.original_expected_end_date is None and rental.expected_end_date is not None:
            rental.original_expected_end_date = rental.expected_end_date

        db.commit()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})
    finally:
        db.close()


@app.route("/api/edit-future-booking", methods=["POST"])
def edit_future_booking():
    """修改預約中的未來租約（日期、車主），並處理退費"""
    db = _db()
    try:
        data = request.get_json()
        rental_id = int(data.get("rental_id", 0))
        refund = int(data.get("refund_amount", 0))
        extra_charge = int(data.get("extra_charge", 0))
        rental = db.query(Rental).get(rental_id)
        if not rental:
            return jsonify({"ok": False, "msg": "找不到出租記錄"})
        today = datetime.date.today()
        if rental.start_date <= today:
            return jsonify({"ok": False, "msg": "此租約已開始，無法修改預約"})
        tenant_id = data.get("tenant_id")
        if tenant_id:
            rental.tenant_id = int(tenant_id)
        start_date = data.get("start_date")
        new_start = datetime.date.fromisoformat(start_date) if start_date else rental.start_date
        expected_end = data.get("expected_end_date")
        new_end = datetime.date.fromisoformat(expected_end) if expected_end else None

        # 檢查同車位其他 active 租約日期區間是否與新日期重疊
        FAR_FUTURE = datetime.date(9999, 12, 31)
        my_start = new_start
        my_end   = new_end or FAR_FUTURE
        for r in db.query(Rental).filter(
            Rental.spot_id == rental.spot_id,
            Rental.status == "active",
            Rental.id != rental.id,
        ).all():
            r_start = r.start_date
            r_end   = r.expected_end_date or FAR_FUTURE
            if not (my_end < r_start or my_start > r_end):
                _r_end_str = r.expected_end_date.strftime("%Y-%m-%d") if r.expected_end_date else "無期限"
                return jsonify({
                    "ok": False,
                    "msg": (
                        f"日期與既有租約衝突：{r.tenant.name}"
                        f"（{r_start} ~ {_r_end_str}）"
                    )
                })

        # 在更新日期前先抓舊值供退費描述使用
        old_start_str = rental.start_date.strftime("%Y-%m-%d") if rental.start_date else ""
        old_end_str   = rental.expected_end_date.strftime("%Y-%m-%d") if rental.expected_end_date else ""
        if start_date:
            rental.start_date = new_start
        rental.expected_end_date = new_end
        new_start_str = rental.start_date.strftime("%Y-%m-%d") if rental.start_date else ""
        new_end_str   = rental.expected_end_date.strftime("%Y-%m-%d") if rental.expected_end_date else ""
        if refund > 0:
            # 待退款暫存在租約上，由車主確認收到退款後才沖銷預繳記錄
            rental.refund_pending = refund
            # 同時建立詳細的待退款 Expense（包含日期變更資訊），供支出/退費管理頁顯示
            today = datetime.date.today()
            spot_num = rental.spot.spot_number
            tenant_name = rental.tenant.name
            refund_desc = f"車位 {spot_num} — {tenant_name} 退費"
            parts = []
            if old_start_str and new_start_str and old_start_str != new_start_str:
                parts.append(f"原起租日 {old_start_str}，新起租日 {new_start_str}")
            if old_end_str and new_end_str and old_end_str != new_end_str:
                parts.append(f"原預計結束 {old_end_str}，新預計結束 {new_end_str}")
            if not parts:
                parts.append("預約調整")
            full_desc = f"{refund_desc}（{'；'.join(parts)}）"
            _apply_refund(db, refund, refund_desc, today, full_desc, confirmed=False, rental_id=rental.id)
        elif extra_charge == 0:
            # 日期未變動或恰好等額，清除舊的待退款
            rental.refund_pending = 0
            # 同時清除舊的未確認退費 Expense（若有）
            db.query(Expense).filter(
                Expense.category == "退費",
                Expense.confirmed == False,
                Expense.rental_id == rental.id,
            ).delete(synchronize_session=False)
        if extra_charge > 0:
            # 延長/起租日提前：待補繳暫存為負數，由「確認補繳款」按鈕處理
            rental.refund_pending = -extra_charge
            db.query(Expense).filter(
                Expense.category == "退費",
                Expense.confirmed == False,
                Expense.rental_id == rental.id,
            ).delete(synchronize_session=False)
        spot_id = rental.spot_id
        db.commit()
        # 自動合併：若編輯後與現任租約日期連續且同電話，合併
        _merge_consecutive_same_phone(db, spot_id)
        return jsonify({"ok": True})
    except Exception as e:
        db.rollback()
        return jsonify({"ok": False, "msg": str(e)})
    finally:
        db.close()


@app.route("/api/confirm-future-refund", methods=["POST"])
def confirm_future_refund():
    """車主確認已收到退款（下任車主）：將既有的待退款 Expense 標記為已確認；
    若無對應的待退款 Expense（舊資料相容路徑）則建立新紀錄。
    Payment 維持原始毛額不動（與現任車主相同模型，避免雙重扣抵）"""
    db = _db()
    try:
        data = request.get_json()
        rental_id = int(data.get("rental_id", 0))
        reason = (data.get("reason") or "").strip()
        rental = db.query(Rental).get(rental_id)
        if not rental:
            return jsonify({"ok": False, "msg": "找不到出租記錄"})
        refund = int(rental.refund_pending or 0)
        if refund > 0:
            # 優先尋找 edit-future-booking 建立的詳細描述待退款 Expense
            existing = db.query(Expense).filter(
                Expense.category == "退費",
                Expense.confirmed == False,
                Expense.rental_id == rental.id,
            ).first()
            if existing:
                existing.confirmed = True
                if reason:
                    existing.description = f"{existing.description}（原因：{reason}）"
            else:
                # fallback：找不到既有未確認 Expense（如：refund_pending 由 auto-sync 設定，
                # 或本筆退款是修改前的舊版本路徑造成）。仍盡量帶上目前日期讓使用者看得出退費對象。
                spot_num = rental.spot.spot_number
                tenant_name = rental.tenant.name
                cur_start = rental.start_date.strftime("%Y-%m-%d") if rental.start_date else "—"
                cur_end   = rental.expected_end_date.strftime("%Y-%m-%d") if rental.expected_end_date else "未設定"
                desc = f"車位 {spot_num} — {tenant_name} 退費（預約退款，目前起租日 {cur_start}，預計結束 {cur_end}）"
                if reason:
                    desc += f"（原因：{reason}）"
                db.add(Expense(
                    expense_date=datetime.date.today(),
                    amount=refund,
                    category="退費",
                    description=desc,
                    confirmed=True,
                    rental_id=rental.id,
                ))
        rental.refund_pending = 0
        db.commit()
        return jsonify({"ok": True})
    except Exception as e:
        db.rollback()
        return jsonify({"ok": False, "msg": str(e)})
    finally:
        db.close()


@app.route("/api/waive-future-refund", methods=["POST"])
def waive_future_refund():
    """車主告知不需退款（下任車主）：清除 refund_pending，建立豁免備查記錄"""
    db = _db()
    try:
        data = request.get_json()
        rental_id = int(data.get("rental_id", 0))
        amount = int(data.get("amount", 0))
        rental = db.query(Rental).get(rental_id)
        if not rental:
            return jsonify({"ok": False, "msg": "找不到出租記錄"})
        rental.refund_pending = 0
        spot_num = rental.spot.spot_number
        tenant_name = rental.tenant.name
        desc = f"車位 {spot_num} — {tenant_name} 退費豁免：車主告知不需退款（預約退款）"
        if amount > 0:
            desc += f"（原計算應退 {amount:,} 元）"
        db.add(Expense(
            expense_date=datetime.date.today(),
            amount=0,
            category="退費豁免",
            description=desc,
            confirmed=True,
            rental_id=rental.id,
        ))
        db.commit()
        return jsonify({"ok": True})
    except Exception as e:
        db.rollback()
        return jsonify({"ok": False, "msg": str(e)})
    finally:
        db.close()


@app.route("/api/cancel-future-booking", methods=["POST"])
def cancel_future_booking():
    """取消尚未開始的預約出租（獨立未來租約），並處理退費"""
    db = _db()
    try:
        data = request.get_json()
        rental_id = int(data.get("rental_id", 0))
        rental = db.query(Rental).get(rental_id)
        if not rental:
            return jsonify({"ok": False, "msg": "找不到出租記錄"})
        today = datetime.date.today()
        if rental.start_date <= today:
            return jsonify({"ok": False, "msg": "此租約已開始，無法取消預約"})
        rental.status = "ended"
        rental.end_date = today
        # 取消預約屬已收帳款全額沖銷，刪除所有預繳記錄，而非新增支出
        for p in list(rental.payments):
            db.delete(p)
        db.commit()
        return jsonify({"ok": True})
    except Exception as e:
        db.rollback()
        return jsonify({"ok": False, "msg": str(e)})
    finally:
        db.close()


@app.route("/api/quick-end", methods=["POST"])
def quick_end():
    """從儀表板快速結束出租（含退費）。cancel_mode=True 為「取消租約」整單作廢，
    退費直接建立為已確認（後續無面板可再確認）。"""
    db = _db()
    try:
        data = request.get_json()
        rental_id = int(data.get("rental_id", 0))
        refund = int(data.get("refund_amount", 0))
        extra_charge = int(data.get("extra_charge", 0))
        end_date_str = data.get("end_date")
        cancel_mode = bool(data.get("cancel_mode", False))
        rental = db.query(Rental).get(rental_id)
        if not rental:
            return jsonify({"ok": False, "msg": "找不到出租記錄"})

        today = datetime.date.today()
        end_date = datetime.date.fromisoformat(end_date_str) if end_date_str else today
        original_end_str = rental.expected_end_date.strftime("%Y-%m-%d") if rental.expected_end_date else ""
        new_end_str = end_date.strftime("%Y-%m-%d")

        # 與下一位車主衝突檢查
        if rental.next_start_date and end_date >= rental.next_start_date:
            return jsonify({"ok": False, "msg": f"退租日 {end_date} 不可晚於或等於下一位車主起租日 {rental.next_start_date}"})
        for fr in db.query(Rental).filter(
            Rental.spot_id == rental.spot_id,
            Rental.status == "active",
            Rental.id != rental.id,
            Rental.start_date > today,
        ).all():
            if end_date >= fr.start_date:
                return jsonify({"ok": False, "msg": f"退租日 {end_date} 不可晚於或等於預約車主 {fr.tenant.name} 的起租日 {fr.start_date}"})

        if end_date <= today and extra_charge == 0:
            # 無補繳爭議：立即結束出租
            rental.status = "ended"
            rental.end_date = end_date
        elif end_date <= today and extra_charge > 0:
            # 有補繳未收：保持 active，待面板「確認補繳」後再結束
            rental.expected_end_date = end_date
            rental.refund_pending = -extra_charge
        else:
            # 未來退租日：補繳暫存為待確認，由面板「確認補繳」按鈕處理
            rental.expected_end_date = end_date
            if extra_charge > 0:
                rental.refund_pending = -extra_charge
            elif not refund:
                rental.refund_pending = 0

        refund_desc = f"車位 {rental.spot.spot_number} — {rental.tenant.name} 退費"
        if cancel_mode:
            full_desc = f"{refund_desc}（取消租約 {new_end_str}）"
        elif original_end_str and original_end_str != new_end_str:
            full_desc = f"{refund_desc}（原退租日 {original_end_str}，新退租日 {new_end_str}）"
        else:
            full_desc = f"{refund_desc}（退租日 {new_end_str}）"
        _apply_refund(db, refund, refund_desc, today, full_desc, confirmed=cancel_mode, rental_id=rental.id)
        db.commit()
        return jsonify({"ok": True, "immediate": end_date <= today})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})
    finally:
        db.close()


@app.route("/api/confirm-rental-charge", methods=["POST"])
def confirm_rental_charge():
    """確認補繳款；amount 可指定部分收款，未填則全額收款。
    若租約有已確認退費，本次補繳優先沖銷退費（避免重複收款並保持帳面乾淨）。"""
    db = _db()
    try:
        data = request.get_json()
        rental_id = int(data.get("rental_id", 0))
        rental = db.query(Rental).get(rental_id)
        if not rental or not rental.refund_pending or rental.refund_pending >= 0:
            return jsonify({"ok": False, "msg": "無待補繳記錄"})
        total_pending = -int(rental.refund_pending)
        raw_amount = data.get("amount")
        pay_amount = int(raw_amount) if raw_amount is not None else total_pending
        pay_amount = max(1, min(pay_amount, total_pending))
        today = datetime.date.today()

        # 先沖銷已確認的退費（每沖銷 1 元 = 等同收回 1 元退費）
        remaining_pay = pay_amount
        confirmed_refunds = db.query(Expense).filter(
            Expense.category == "退費",
            Expense.confirmed == True,
            Expense.rental_id == rental.id,
        ).order_by(Expense.expense_date.desc()).all()
        for refund in confirmed_refunds:
            if remaining_pay <= 0:
                break
            r_amt = int(refund.amount)
            if r_amt <= remaining_pay:
                remaining_pay -= r_amt
                db.delete(refund)
            else:
                refund.amount = r_amt - remaining_pay
                remaining_pay = 0

        # 剩餘金額才建立新的繳費紀錄；自動拆成月繳（一個月一筆 = 月租金），
        # 不滿月的尾數以實收金額記錄；遇已有的 period_month 自動跳過。
        if remaining_pay > 0:
            existing = {p.period_month for p in rental.payments}
            if existing:
                last_pm = max(existing)
                y, m = int(last_pm[:4]), int(last_pm[5:7])
                m += 1
                if m > 12: m = 1; y += 1
            else:
                y, m = rental.start_date.year, rental.start_date.month
            fee = int(rental.monthly_fee or 1000)
            remain = remaining_pay
            guard = 0
            while remain > 0 and guard < 600:  # 防呆上限：50 年
                guard += 1
                pm = f"{y:04d}-{m:02d}"
                if pm not in existing:
                    pay_amt = fee if remain >= fee else remain
                    db.add(Payment(
                        rental_id=rental.id,
                        payment_date=today,
                        amount=pay_amt,
                        period_month=pm,
                    ))
                    existing.add(pm)
                    remain -= pay_amt
                m += 1
                if m > 12: m = 1; y += 1
        remaining = total_pending - pay_amount
        rental.refund_pending = -remaining if remaining > 0 else 0
        # 若此筆補繳是為了結束出租（end_date <= today）且已全額收清，順便結束租約
        if remaining == 0 and rental.expected_end_date and rental.expected_end_date <= today:
            rental.status = "ended"
            rental.end_date = rental.expected_end_date
        db.commit()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})
    finally:
        db.close()


@app.route("/api/cancel-charge-waiver", methods=["POST"])
def cancel_charge_waiver():
    """取消（刪除）某租約的所有「補繳豁免」記錄；用於誤按豁免後恢復補繳狀態。
    取消後重新計算 gap，將 rental.refund_pending 設為 -gap，使補繳/部分補繳/豁免按鈕重新出現。"""
    db = _db()
    try:
        data = request.get_json()
        rental_id = int(data.get("rental_id", 0))
        rental = db.query(Rental).get(rental_id)
        if not rental:
            return jsonify({"ok": False, "msg": "找不到租約"})
        count = db.query(Expense).filter(
            Expense.category == "補繳豁免",
            Expense.rental_id == rental.id,
        ).delete(synchronize_session=False)
        db.flush()
        # 重新計算 gap：應繳 - 淨繳（不再有豁免抵免）
        if rental.expected_end_date:
            gross_paid = int(sum(p.amount for p in rental.payments))
            confirmed_refund = int(db.query(
                func.coalesce(func.sum(Expense.amount), 0)
            ).filter(
                Expense.category == "退費",
                Expense.confirmed == True,
                Expense.rental_id == rental.id,
            ).scalar() or 0)
            net_paid = gross_paid - confirmed_refund
            fee = int(rental.monthly_fee or 1000)
            expected = _calc_calendar_months_total(rental.start_date, rental.expected_end_date, fee)
            gap = expected - net_paid
            if gap > 0:
                rental.refund_pending = -gap
            else:
                rental.refund_pending = 0
        db.commit()
        return jsonify({"ok": True, "deleted": count})
    except Exception as e:
        db.rollback()
        return jsonify({"ok": False, "msg": str(e)})
    finally:
        db.close()


@app.route("/api/waive-rental-charge", methods=["POST"])
def waive_rental_charge():
    """豁免待補繳款：建立支出備查記錄並清除 refund_pending"""
    db = _db()
    try:
        data = request.get_json()
        rental_id = int(data.get("rental_id", 0))
        rental = db.query(Rental).get(rental_id)
        if not rental:
            return jsonify({"ok": False, "msg": "找不到出租記錄"})
        pending = -int(rental.refund_pending or 0)
        if pending <= 0:
            return jsonify({"ok": False, "msg": "無待補繳記錄"})
        spot_num = rental.spot.spot_number
        tenant_name = rental.tenant.name
        db.add(Expense(
            expense_date=datetime.date.today(),
            amount=pending,
            category="補繳豁免",
            description=f"車位 {spot_num} — {tenant_name} 補繳豁免（房東免收 {pending:,} 元）",
            confirmed=True,
            rental_id=rental.id,
        ))
        rental.refund_pending = 0
        # 若豁免的是退租補繳，且退租日已過，順便結束租約
        if rental.expected_end_date and rental.expected_end_date <= datetime.date.today():
            rental.status = "ended"
            rental.end_date = rental.expected_end_date
        db.commit()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})
    finally:
        db.close()


@app.route("/api/transfer-rental", methods=["POST"])
def transfer_rental():
    """設定下一位車主：起租日未到則預約（保留現租約），已到則立即換租"""
    db = _db()
    try:
        data = request.get_json()
        rental_id    = int(data.get("rental_id", 0))
        tenant_id    = int(data.get("tenant_id", 0))
        start_date   = data.get("start_date") or str(datetime.date.today())
        expected_end = data.get("expected_end_date") or None
        refund          = int(data.get("refund_amount", 0))
        prepaid_months  = max(1, int(data.get("prepaid_months", 1)))

        rental = db.query(Rental).get(rental_id)
        if not rental:
            return jsonify({"ok": False, "msg": "找不到出租記錄"})
        new_tenant = db.query(Tenant).get(tenant_id)
        if not new_tenant:
            return jsonify({"ok": False, "msg": "找不到車主"})

        today = datetime.date.today()
        start_d = datetime.date.fromisoformat(start_date)

        refund_desc = f"車位 {rental.spot.spot_number} — {rental.tenant.name} 退費"
        original_end_str = rental.expected_end_date.strftime("%Y-%m-%d") if rental.expected_end_date else ""

        if start_d > today:
            # 預約換租：存入當前租約，排程到期自動執行
            rental.next_tenant_id = tenant_id
            rental.next_start_date = start_d
            rental.next_expected_end_date = (
                datetime.date.fromisoformat(expected_end) if expected_end else None
            )
            rental.next_prepaid_months = prepaid_months
            # 現任到期日 = 下一位起租日的前一天（避免同天顯示衝突）
            rental.expected_end_date = start_d - datetime.timedelta(days=1)
            new_end_str = rental.expected_end_date.strftime("%Y-%m-%d")
            if original_end_str and original_end_str != new_end_str:
                full_desc = f"{refund_desc}（原退租日 {original_end_str}，新退租日 {new_end_str}）"
            else:
                full_desc = f"{refund_desc}（退租日 {new_end_str}）"
            _apply_refund(db, refund, refund_desc, today, full_desc)

            spot_id = rental.spot_id
            db.commit()
            # 自動合併：若預約對象與現任同電話且日期連續，合併為單一租約
            _merge_consecutive_same_phone(db, spot_id)
            return jsonify({"ok": True, "deferred": True})
        else:
            # 立即換租
            rental.status = "ended"
            rental.end_date = today
            new_end_str = today.strftime("%Y-%m-%d")
            if original_end_str and original_end_str != new_end_str:
                full_desc = f"{refund_desc}（原退租日 {original_end_str}，新退租日 {new_end_str}）"
            else:
                full_desc = f"{refund_desc}（退租日 {new_end_str}）"
            _apply_refund(db, refund, refund_desc, today, full_desc)
            _new_end_d = datetime.date.fromisoformat(expected_end) if expected_end else None
            new_rental = Rental(
                spot_id=rental.spot_id,
                tenant_id=tenant_id,
                start_date=start_d,
                monthly_fee=1000,
                expected_end_date=_new_end_d,
                original_expected_end_date=_new_end_d,
            )
            db.add(new_rental)
            db.flush()
            for i in range(prepaid_months):
                y = start_d.year + (start_d.month - 1 + i) // 12
                m = (start_d.month - 1 + i) % 12 + 1
                db.add(Payment(
                    rental_id=new_rental.id,
                    payment_date=today,
                    amount=1000,
                    period_month=f"{y:04d}-{m:02d}",
                ))
            db.commit()
            return jsonify({"ok": True, "deferred": False})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})
    finally:
        db.close()


@app.route("/api/confirm-next-prepaid", methods=["POST"])
def confirm_next_prepaid():
    """將「預約換租」的收款轉為獨立未來租約。
    mode:
      full    — 全額收款，建立完整 Payment 記錄
      partial — 部分收款，建立部分 Payment + 設定 refund_pending = -(剩餘)
      waive   — 豁免，無 Payment + 建立 補繳豁免 Expense
    """
    db = _db()
    try:
        data = request.get_json()
        rental_id   = int(data.get("rental_id", 0))
        months      = int(data.get("months", 0))
        mode        = (data.get("mode") or "full").lower()
        partial_amt = int(data.get("partial_amount", 0))
        last_amount = int(data.get("last_amount", 0)) or None
        rental = db.query(Rental).get(rental_id)
        if not rental or not rental.next_tenant_id:
            return jsonify({"ok": False, "msg": "找不到預約換租記錄"})

        today   = datetime.date.today()
        start_d = rental.next_start_date
        exp_end = rental.next_expected_end_date
        fee     = int(rental.monthly_fee)
        full_total = months * fee

        # 建立獨立未來租約
        new_rental = Rental(
            spot_id       = rental.spot_id,
            tenant_id     = rental.next_tenant_id,
            start_date    = start_d,
            monthly_fee   = fee,
            expected_end_date = exp_end,
            original_expected_end_date = exp_end,
        )
        db.add(new_rental)
        db.flush()

        spot_num    = rental.spot.spot_number
        next_tenant = db.query(Tenant).get(rental.next_tenant_id)
        tenant_name = next_tenant.name if next_tenant else ""

        if mode == "waive":
            # 不建立 Payment；建立 補繳豁免 支出記錄
            db.add(Expense(
                expense_date=today,
                amount=full_total,
                category="補繳豁免",
                description=f"車位 {spot_num} — {tenant_name} 補繳豁免（預約換租）",
                confirmed=True,
                rental_id=new_rental.id,
            ))
        elif mode == "partial":
            if partial_amt <= 0 or partial_amt >= full_total:
                # 邊界情況退回 full / waive
                if partial_amt >= full_total:
                    mode = "full"
                else:
                    return jsonify({"ok": False, "msg": "部分收款金額無效"})
            if mode == "partial":
                # 從第一個月開始填，每月最多收 fee 元，最後一個有部分金額的月份記錄實收
                remain = partial_amt
                for i in range(months):
                    if remain <= 0:
                        break
                    y = start_d.year + (start_d.month - 1 + i) // 12
                    m = (start_d.month - 1 + i) % 12 + 1
                    pay_amt = fee if remain >= fee else remain
                    db.add(Payment(
                        rental_id    = new_rental.id,
                        payment_date = today,
                        amount       = pay_amt,
                        period_month = f"{y:04d}-{m:02d}",
                    ))
                    remain -= pay_amt
                # 剩餘待補繳記錄為負（表示尚需補繳）
                new_rental.refund_pending = -(full_total - partial_amt)

        if mode == "full":
            # 建立完整預繳付款記錄
            for i in range(months):
                y = start_d.year + (start_d.month - 1 + i) // 12
                m = (start_d.month - 1 + i) % 12 + 1
                is_last = (i == months - 1)
                db.add(Payment(
                    rental_id    = new_rental.id,
                    payment_date = today,
                    amount       = (last_amount if is_last and last_amount else fee),
                    period_month = f"{y:04d}-{m:02d}",
                ))

        # 清除原租約上的預約換租欄位
        rental.next_tenant_id          = None
        rental.next_start_date         = None
        rental.next_expected_end_date  = None
        rental.next_prepaid_months     = None

        db.commit()
        # 自動合併：同電話且連續日期的租約
        _merge_consecutive_same_phone(db, rental.spot_id)
        return jsonify({"ok": True})
    except Exception as e:
        db.rollback()
        return jsonify({"ok": False, "msg": str(e)})
    finally:
        db.close()


@app.route("/api/update-next-tenant", methods=["POST"])
def update_next_tenant():
    """編輯預約換租的車主、起租日、預計結束日"""
    db = _db()
    try:
        data = request.get_json()
        rental_id = int(data.get("rental_id", 0))
        tenant_id = int(data.get("tenant_id", 0))
        start_str = (data.get("start_date") or "").strip()
        end_str   = (data.get("expected_end_date") or "").strip() if data.get("expected_end_date") else ""
        rental = db.query(Rental).get(rental_id)
        if not rental or not rental.next_tenant_id:
            return jsonify({"ok": False, "msg": "找不到預約換租資料"})
        if not tenant_id:
            return jsonify({"ok": False, "msg": "請指定車主"})
        if not start_str:
            return jsonify({"ok": False, "msg": "請指定起租日"})
        try:
            new_start = datetime.datetime.strptime(start_str, "%Y-%m-%d").date()
        except Exception:
            return jsonify({"ok": False, "msg": "起租日格式錯誤"})
        new_end = None
        if end_str:
            try:
                new_end = datetime.datetime.strptime(end_str, "%Y-%m-%d").date()
            except Exception:
                return jsonify({"ok": False, "msg": "預計結束日格式錯誤"})
        # 起租日必須晚於現任租約起租日
        if new_start <= rental.start_date:
            return jsonify({"ok": False, "msg": f"起租日必須晚於現任車主起租日（{rental.start_date}）"})
        if new_end and new_end < new_start:
            return jsonify({"ok": False, "msg": "預計結束日必須晚於起租日"})
        rental.next_tenant_id = tenant_id
        rental.next_start_date = new_start
        rental.next_expected_end_date = new_end
        # 重新計算 next_prepaid_months（依日曆月計算）
        if new_end:
            months = (new_end.year - new_start.year) * 12 + (new_end.month - new_start.month) + 1
            rental.next_prepaid_months = max(1, months)
        db.commit()
        return jsonify({"ok": True})
    except Exception as e:
        db.rollback()
        return jsonify({"ok": False, "msg": str(e)})
    finally:
        db.close()


@app.route("/api/create-pending-refund", methods=["POST"])
def create_pending_refund():
    """重建待確認退費記錄（當退費記錄被手動刪除後，重新開啟 modal 自動呼叫）"""
    db = _db()
    try:
        data = request.get_json()
        rental_id = int(data.get("rental_id", 0))
        amount = int(data.get("amount", 0))
        rental = db.query(Rental).get(rental_id)
        if not rental:
            return jsonify({"ok": False, "msg": "找不到出租記錄"})
        if amount <= 0:
            return jsonify({"ok": False, "msg": "金額必須大於 0"})
        today = datetime.date.today()
        spot_num = rental.spot.spot_number
        tenant_name = rental.tenant.name
        desc_prefix = f"車位 {spot_num} — {tenant_name} 退費"
        # 避免重複建立
        existing = db.query(Expense).filter(
            Expense.category == "退費",
            Expense.confirmed == False,
            Expense.rental_id == rental.id,
        ).first()
        if existing:
            return jsonify({"ok": True})
        db.add(Expense(
            expense_date=today,
            amount=amount,
            category="退費",
            description=f"{desc_prefix}（待確認）",
            confirmed=False,
            rental_id=rental.id,
        ))
        db.commit()
        return jsonify({"ok": True})
    except Exception as e:
        db.rollback()
        return jsonify({"ok": False, "msg": str(e)})
    finally:
        db.close()


@app.route("/api/confirm-refund", methods=["POST"])
def confirm_refund():
    """標記退費記錄為已退款"""
    db = _db()
    try:
        data = request.get_json()
        expense_id = int(data.get("expense_id", 0))
        reason = (data.get("reason") or "").strip()
        exp = db.query(Expense).get(expense_id)
        if not exp:
            return jsonify({"ok": False, "msg": "找不到退費記錄"})
        exp.confirmed = True
        if reason:
            exp.description = f"{exp.description}（原因：{reason}）"
        db.commit()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})
    finally:
        db.close()


@app.route("/api/cancel-transfer", methods=["POST"])
def cancel_transfer():
    """取消預約換租"""
    db = _db()
    try:
        data = request.get_json()
        rental_id = int(data.get("rental_id", 0))
        rental = db.query(Rental).get(rental_id)
        if not rental:
            return jsonify({"ok": False, "msg": "找不到出租記錄"})
        rental.next_tenant_id = None
        rental.next_start_date = None
        rental.next_expected_end_date = None
        rental.next_prepaid_months = None
        db.query(Expense).filter(
            Expense.category == "退費",
            Expense.confirmed == False,
            Expense.rental_id == rental.id,
        ).delete(synchronize_session=False)
        db.commit()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})
    finally:
        db.close()


@app.route("/api/waive-refund", methods=["POST"])
def waive_refund():
    """車主告知不需退款：清除待確認退費，建立豁免備查記錄（不計入支出）"""
    db = _db()
    try:
        data = request.get_json()
        rental_id = int(data.get("rental_id", 0))
        amount = int(data.get("amount", 0))
        rental = db.query(Rental).get(rental_id)
        if not rental:
            return jsonify({"ok": False, "error": "找不到出租記錄"})
        spot_num = rental.spot.spot_number
        tenant_name = rental.tenant.name
        original_end_date = data.get("original_end_date", "")
        modified_end_date = data.get("modified_end_date", "")
        db.query(Expense).filter(
            Expense.category == "退費",
            Expense.confirmed == False,
            Expense.rental_id == rental.id,
        ).delete(synchronize_session=False)
        parts = []
        if original_end_date:
            parts.append(f"原退租日 {original_end_date}")
        if modified_end_date:
            parts.append(f"新退租日 {modified_end_date}")
        if amount > 0:
            parts.append(f"原計算應退 {amount:,} 元")
        desc = f"車位 {spot_num} — {tenant_name} 退費豁免：車主告知不需退款"
        if parts:
            desc += f"（{'，'.join(parts)}）"
        db.add(Expense(
            expense_date=datetime.date.today(),
            amount=0,
            category="退費豁免",
            description=desc,
            confirmed=True,
            rental_id=rental.id,
        ))
        db.commit()
        return jsonify({"ok": True})
    except Exception as e:
        db.rollback()
        return jsonify({"ok": False, "error": str(e)})
    finally:
        db.close()


@app.route("/api/vacant-spots", methods=["GET"])
def vacant_spots():
    """回傳可用車位。若提供 rental_id 參數，則以該租約日期區間判斷無衝突的車位（適用未來預約移動）；
    否則回傳目前空置車位（適用現任租約移動）。"""
    db = _db()
    try:
        rental_id_str = request.args.get("rental_id")
        target_rental = None
        if rental_id_str:
            try:
                target_rental = db.get(Rental, int(rental_id_str))
            except Exception:
                target_rental = None
        spots = db.query(ParkingSpot).filter_by(spot_type="rental", is_active=True).order_by(ParkingSpot.spot_number).all()
        today = datetime.date.today()
        FAR_FUTURE = datetime.date(9999, 12, 31)
        result = []
        for s in spots:
            if target_rental:
                # 跳過租約自身所在的車位
                if s.id == target_rental.spot_id:
                    continue
                # 日期區間衝突判斷
                my_start = target_rental.start_date
                my_end = target_rental.expected_end_date or FAR_FUTURE
                conflict = False
                for r in s.rentals:
                    if r.status != "active" or r.id == target_rental.id:
                        continue
                    r_start = r.start_date
                    r_end = r.expected_end_date or FAR_FUTURE
                    if not (my_end < r_start or my_start > r_end):
                        conflict = True
                        break
                if not conflict:
                    result.append({"id": s.id, "spot_number": s.spot_number})
            else:
                status, _ = spot_status(s, today)
                if status == "vacant":
                    result.append({"id": s.id, "spot_number": s.spot_number})
        return jsonify({"ok": True, "spots": result})
    finally:
        db.close()


@app.route("/api/move-rental", methods=["POST"])
def move_rental():
    """將租約（含未來預約）移至另一個車位；以日期區間衝突判斷"""
    db = _db()
    try:
        data = request.get_json()
        rental_id = int(data.get("rental_id", 0))
        new_spot_id = int(data.get("spot_id", 0))
        rental = db.get(Rental, rental_id)
        if not rental or rental.status != "active":
            return jsonify({"ok": False, "error": "租約不存在或已結束"})
        new_spot = db.get(ParkingSpot, new_spot_id)
        if not new_spot or new_spot.spot_type != "rental":
            return jsonify({"ok": False, "error": "目標車位不存在"})
        FAR_FUTURE = datetime.date(9999, 12, 31)
        my_start = rental.start_date
        my_end = rental.expected_end_date or FAR_FUTURE
        # 檢查目標車位現有 active 租約是否與本租約日期區間重疊
        for r in db.query(Rental).filter(
            Rental.spot_id == new_spot_id,
            Rental.status == "active",
            Rental.id != rental.id,
        ).all():
            r_start = r.start_date
            r_end = r.expected_end_date or FAR_FUTURE
            if not (my_end < r_start or my_start > r_end):
                return jsonify({
                    "ok": False,
                    "error": (
                        f"目標車位 {new_spot.spot_number} 與既有租約衝突："
                        f"{r.tenant.name}（{r_start} ~ "
                        f"{r.expected_end_date.strftime('%Y-%m-%d') if r.expected_end_date else '無期限'}）"
                    )
                })
        # 同步更新本租約相關 Expense（退費／退費豁免／補繳豁免）的描述前綴：
        # 描述以「車位 {編號} — {車主名}」識別退費歷史，移位後若不更新會：
        # (1) 新車位查不到歷史；(2) 舊車位若有同車主新租約會誤抓到舊紀錄。
        old_spot_num = rental.spot.spot_number
        new_spot_num = new_spot.spot_number
        if old_spot_num != new_spot_num:
            old_prefix = f"車位 {old_spot_num} — {rental.tenant.name}"
            new_prefix = f"車位 {new_spot_num} — {rental.tenant.name}"
            for exp in db.query(Expense).filter(
                Expense.description.like(f"{old_prefix}%")
            ).all():
                exp.description = new_prefix + exp.description[len(old_prefix):]
        rental.spot_id = new_spot_id
        db.commit()
        return jsonify({"ok": True})
    except Exception as e:
        db.rollback()
        return jsonify({"ok": False, "error": str(e)})
    finally:
        db.close()


@app.route("/api/next-tenant-prepaid", methods=["POST"])
def next_tenant_prepaid():
    """修改預約換租的預繳月數"""
    db = _db()
    try:
        data = request.get_json()
        rental_id = int(data.get("rental_id", 0))
        months = max(1, int(data.get("months", 1)))
        rental = db.query(Rental).get(rental_id)
        if not rental or not rental.next_tenant_id:
            return jsonify({"ok": False, "error": "找不到預約換租資料"})
        rental.next_prepaid_months = months
        db.commit()
        return jsonify({"ok": True, "months": months})
    except Exception as e:
        db.rollback()
        return jsonify({"ok": False, "error": str(e)})
    finally:
        db.close()


@app.route("/api/transfer-to-vacant", methods=["POST"])
def transfer_to_vacant():
    """將預約換租移至其他空置車位：立即建立新租約並清除預約"""
    db = _db()
    try:
        data = request.get_json()
        rental_id = int(data.get("rental_id", 0))
        spot_id   = int(data.get("spot_id", 0))
        rental = db.query(Rental).get(rental_id)
        if not rental or not rental.next_tenant_id:
            return jsonify({"ok": False, "error": "找不到預約換租資料"})
        target_spot = db.query(ParkingSpot).get(spot_id)
        if not target_spot:
            return jsonify({"ok": False, "error": "找不到目標車位"})
        today = datetime.date.today()
        # 驗證目標車位空置
        target_status, _ = spot_status(target_spot, today)
        if target_status != "vacant":
            return jsonify({"ok": False, "error": f"車位 {target_spot.spot_number} 目前非空置"})

        start_d = rental.next_start_date or today
        if start_d < today:
            start_d = today
        n_months = rental.next_prepaid_months or 0

        # 在目標車位建立新租約
        new_rental = Rental(
            spot_id=target_spot.id,
            tenant_id=rental.next_tenant_id,
            start_date=start_d,
            monthly_fee=1000,
            expected_end_date=rental.next_expected_end_date,
            original_expected_end_date=rental.next_expected_end_date,
            status="active",
        )
        db.add(new_rental)
        db.flush()
        # 建立預繳記錄
        for i in range(n_months):
            y = start_d.year  + (start_d.month - 1 + i) // 12
            m = (start_d.month - 1 + i) % 12 + 1
            db.add(Payment(
                rental_id=new_rental.id,
                payment_date=today,
                amount=1000,
                period_month=f"{y:04d}-{m:02d}",
            ))

        # 清除當前租約的預約資料
        rental.next_tenant_id = None
        rental.next_start_date = None
        rental.next_expected_end_date = None
        rental.next_prepaid_months = None
        # 有已確認退費 → 使用者曾手動設定提前退租，保留 expected_end_date 不變
        # 無已確認退費 → 恢復至繳費紀錄推算的自然到期日
        has_confirmed_refund = db.query(Expense).filter(
            Expense.category == "退費",
            Expense.confirmed == True,
            Expense.rental_id == rental.id,
        ).count() > 0
        if not has_confirmed_refund and rental.payments:
            max_period = max(p.period_month for p in rental.payments)
            rental.expected_end_date = _month_end(max_period)
        # 清除相關未確認退費記錄
        db.query(Expense).filter(
            Expense.category == "退費",
            Expense.confirmed == False,
            Expense.rental_id == rental.id,
        ).delete(synchronize_session=False)

        db.commit()
        return jsonify({"ok": True, "new_spot": target_spot.spot_number})
    except Exception as e:
        db.rollback()
        return jsonify({"ok": False, "error": str(e)})
    finally:
        db.close()


@app.route("/api/spot-reorder", methods=["POST"])
def spot_reorder():
    """更新車位排序：body { ids: [3, 1, 2, ...] } 依此順序設定 sort_order"""
    db = _db()
    try:
        data = request.get_json()
        ids = data.get("ids") or []
        if not isinstance(ids, list):
            return jsonify({"ok": False, "msg": "ids 必須是陣列"})
        for idx, sid in enumerate(ids):
            try:
                spot_id = int(sid)
            except (TypeError, ValueError):
                continue
            spot = db.query(ParkingSpot).get(spot_id)
            if spot:
                spot.sort_order = idx
        db.commit()
        return jsonify({"ok": True})
    except Exception as e:
        db.rollback()
        return jsonify({"ok": False, "msg": str(e)})
    finally:
        db.close()


@app.route("/api/spot-type", methods=["POST"])
def spot_type_toggle():
    """切換車位類型 rental ↔ disabled；停用時可附原因"""
    db = _db()
    try:
        data = request.get_json()
        spot_id = int(data.get("spot_id", 0))
        new_type = data.get("spot_type", "rental")
        reason = (data.get("reason") or "").strip()
        if new_type not in ("rental", "disabled"):
            return jsonify({"ok": False, "msg": "無效的類型"})
        spot = db.query(ParkingSpot).get(spot_id)
        if not spot:
            return jsonify({"ok": False, "msg": "找不到車位"})
        if new_type == "disabled" and any(r.status == "active" for r in spot.rentals):
            return jsonify({"ok": False, "msg": "請先結束出租再變更類型"})
        spot.spot_type = new_type
        if new_type == "disabled":
            spot.disabled_reason = reason or None
        else:
            spot.disabled_reason = None
        db.commit()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})
    finally:
        db.close()


# ─── 支出管理 ─────────────────────────────────────────────────────────────────

EXPENSE_CATEGORIES = ["油漆", "修補", "清潔", "水電", "設備", "退費", "補繳豁免", "其他"]


@app.route("/expenses")
def expenses():
    db = _db()
    try:
        month_filter = request.args.get("month", current_month())
        if month_filter == "all":
            all_expenses = db.query(Expense).order_by(Expense.expense_date.desc()).all()
        else:
            all_expenses = (
                db.query(Expense)
                .filter(func.strftime("%Y-%m", Expense.expense_date) == month_filter)
                .order_by(Expense.expense_date.desc())
                .all()
            )
        return render_template(
            "expenses.html",
            expenses=all_expenses,
            month_filter=month_filter,
            categories=EXPENSE_CATEGORIES,
            today=datetime.date.today(),
        )
    finally:
        db.close()


@app.route("/expenses/add", methods=["POST"])
def expense_add():
    db = _db()
    try:
        amount = request.form.get("amount", "0")
        expense_date = request.form.get("expense_date") or str(datetime.date.today())
        category = request.form.get("category", "其他")
        description = request.form.get("description", "").strip()

        if int(float(amount)) <= 0:
            flash("金額必須大於 0", "warning")
            return redirect(url_for("expenses"))

        db.add(Expense(
            expense_date=datetime.date.fromisoformat(expense_date),
            amount=int(float(amount)),
            category=category,
            description=description or None,
        ))
        db.commit()
        flash("支出記錄已新增", "success")
    finally:
        db.close()
    return redirect(url_for("expenses"))


@app.route("/expenses/<int:expense_id>/confirm-refund", methods=["POST"])
def confirm_refund_page(expense_id):
    db = _db()
    try:
        exp = db.query(Expense).get(expense_id)
        if exp:
            exp.confirmed = True
            db.commit()
            flash("已標記退款完成", "success")
    finally:
        db.close()
    return redirect(request.referrer or url_for("expenses"))


@app.route("/expenses/<int:expense_id>/edit", methods=["GET", "POST"])
def expense_edit(expense_id):
    db = _db()
    try:
        exp = db.query(Expense).get(expense_id)
        if not exp:
            flash("找不到該記錄", "danger")
            return redirect(url_for("expenses"))
        if request.method == "POST":
            exp.expense_date = datetime.date.fromisoformat(
                request.form.get("expense_date", str(exp.expense_date))
            )
            exp.amount = int(float(request.form.get("amount", exp.amount)))
            exp.category = request.form.get("category", exp.category)
            exp.description = request.form.get("description", "").strip() or None
            db.commit()
            flash("支出記錄已更新", "success")
            return redirect(url_for("expenses"))
        return render_template("expense_form.html", exp=exp, categories=EXPENSE_CATEGORIES)
    finally:
        db.close()


@app.route("/expenses/<int:expense_id>/delete", methods=["POST"])
def expense_delete(expense_id):
    db = _db()
    try:
        exp = db.query(Expense).get(expense_id)
        if exp:
            db.delete(exp)
            db.commit()
            flash("支出記錄已刪除", "success")
    finally:
        db.close()
    return redirect(url_for("expenses"))


# ─── Line 通知 ────────────────────────────────────────────────────────────────

@app.route("/api/test-notify", methods=["POST"])
def test_notify():
    """發送一則測試訊息，確認 LINE 推播設定是否正確"""
    if not line_status()["enabled"]:
        return jsonify({"ok": False, "msg": "尚未設定 LINE_CHANNEL_ACCESS_TOKEN 或 LINE_USER_ID"})
    ok = send_line_notify(
        f"🔔 停車場管理系統 — LINE 通知測試\n"
        f"時間：{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"設定正確，未來通知會推送到此聊天室。"
    )
    return jsonify({"ok": ok, "msg": "測試訊息已發送" if ok else "發送失敗，請檢查 Token / User ID 與網路"})


@app.route("/api/run-notify/<job>", methods=["POST"])
def run_notify(job):
    """手動觸發某個排程任務（不等到隔天 09:00 也能立即跑一次）"""
    if not line_status()["enabled"]:
        return jsonify({"ok": False, "msg": "尚未設定 LINE 推播"})
    try:
        if job == "unpaid":
            count = check_unpaid_rentals() or 0
            return jsonify({"ok": True, "msg": f"已執行未繳費檢查（{count} 筆短繳）"})
        elif job == "expiring":
            count = check_expiring_rentals() or 0
            return jsonify({"ok": True, "msg": f"已執行合約到期檢查（送出 {count} 則通知）"})
        elif job == "refund":
            count = check_pending_refunds() or 0
            return jsonify({"ok": True, "msg": f"已執行待退款檢查（{count} 筆）"})
        elif job == "auto_renew":
            count = check_auto_renew_upcoming() or 0
            return jsonify({"ok": True, "msg": f"已執行換手預告檢查（{count} 筆）"})
        elif job == "monthly_summary":
            check_monthly_summary()
            return jsonify({"ok": True, "msg": "已發送每月收支摘要"})
        else:
            return jsonify({"ok": False, "msg": f"未知任務：{job}"})
    except Exception as e:
        return jsonify({"ok": False, "msg": f"執行失敗：{e}"})


# ─── 設定 / 備份還原 ──────────────────────────────────────────────────────────────

@app.route("/settings")
def settings():
    tiered_restore_points = bk.get_tiered_restore_points()
    all_backups = bk.list_all_backups()
    return render_template("settings.html",
                           tiered_restore_points=tiered_restore_points,
                           all_backups=all_backups)


@app.route("/settings/notify", methods=["POST"])
def save_notify():
    """儲存通知偏好（啟用 / 時間），即時套用至排程"""
    try:
        form = request.form.to_dict()
        save_notify_settings(form)
        save_simple_notify_settings(form)
        flash("通知設定已儲存並套用", "success")
    except Exception as e:
        flash(f"儲存失敗：{e}", "danger")
    return redirect(url_for("schedule"))


# ─── 排程設定 ────────────────────────────────────────────────────────────────

@app.route("/schedule")
def schedule():
    """通知與排程設定統一頁面"""
    return render_template(
        "schedule.html",
        line_info=line_status(),
        notify_settings=get_notify_settings(),
        simple_notify_settings=get_simple_notify_settings(),
        backup_interval=get_backup_interval(),
    )


@app.route("/schedule/backup", methods=["POST"])
def save_backup_schedule():
    """儲存自動備份間隔"""
    try:
        save_backup_settings(request.form.to_dict())
        flash("自動備份排程已更新", "success")
    except Exception as e:
        flash(f"儲存失敗：{e}", "danger")
    return redirect(url_for("schedule"))


@app.route("/settings/backup-now", methods=["POST"])
def backup_now():
    try:
        path = bk.create_backup()
        flash(f"備份成功：{path.name}", "success")
    except Exception as e:
        flash(f"備份失敗：{e}", "danger")
    return redirect(url_for("settings"))


@app.route("/settings/restore", methods=["POST"])
def restore():
    backup_path = request.form.get("backup_path", "").strip()
    if not backup_path:
        flash("未指定備份檔案", "danger")
        return redirect(url_for("settings"))
    import os
    if not os.path.isfile(backup_path):
        flash("找不到備份檔案", "danger")
        return redirect(url_for("settings"))
    try:
        bk.restore_from(backup_path)
        flash(f"還原成功！資料已還原至備份：{os.path.basename(backup_path)}", "success")
    except Exception as e:
        flash(f"還原失敗：{e}", "danger")
    return redirect(url_for("settings"))


# ─── 臨時診斷路由（確認 iOS file input 是否正常）─────────────────────────────────
@app.route("/upload-test", methods=["GET","POST"])
def upload_test():
    if request.method == "POST":
        f = request.files.get("f")
        return f"<h2>收到檔案：{f.filename if f else '無'}</h2><a href='/upload-test'>返回</a>"
    return """<!DOCTYPE html><html><head>
<meta name="viewport" content="width=device-width,initial-scale=1">
</head><body style="padding:30px;font-size:1.2rem;">
<h3>iOS 上傳測試</h3>
<form method="post" enctype="multipart/form-data">
  <p>請點下方按鈕選擇照片：</p>
  <input type="file" name="f" accept="image/*" style="font-size:1rem;margin-bottom:20px;display:block"><br>
  <button type="submit" style="padding:14px 30px;font-size:1rem;background:#007bff;color:#fff;border:none;border-radius:8px">上傳</button>
</form></body></html>"""

# ─── LINE Webhook & 收件人管理 ────────────────────────────────────────────────

@app.route("/line/webhook", methods=["POST"])
def line_webhook():
    """接收 LINE 事件，自動記錄傳訊者的 user/group ID"""
    body = request.get_json(force=True, silent=True) or {}
    db = SessionLocal()
    try:
        for event in body.get("events", []):
            source = event.get("source", {})
            src_type = source.get("type")
            if src_type == "group":
                target_id   = source.get("groupId")
                target_type = "group"
                default_name = f"群組 {target_id[:10]}…"
            else:
                target_id   = source.get("userId")
                target_type = "user"
                default_name = f"個人 {target_id[:10]}…" if target_id else None
            if target_id and not db.query(LineTarget).filter_by(target_id=target_id).first():
                db.add(LineTarget(
                    target_id=target_id,
                    target_type=target_type,
                    display_name=default_name,
                    enabled=False,  # 管理員需手動啟用
                ))
        db.commit()
    except Exception as e:
        print(f"[webhook] 處理失敗：{e}")
    finally:
        db.close()
    return jsonify({"ok": True})


@app.route("/api/line-targets", methods=["GET"])
def api_line_targets_list():
    db = SessionLocal()
    try:
        targets = db.query(LineTarget).order_by(LineTarget.id).all()
        return jsonify([{
            "id": t.id,
            "target_id": t.target_id,
            "target_type": t.target_type,
            "display_name": t.display_name or "",
            "enabled": t.enabled,
        } for t in targets])
    finally:
        db.close()


@app.route("/api/line-targets/<int:tid>", methods=["PATCH"])
def api_line_targets_update(tid):
    db = SessionLocal()
    try:
        t = db.query(LineTarget).get(tid)
        if not t:
            return jsonify({"ok": False, "msg": "找不到"}), 404
        data = request.get_json(force=True, silent=True) or {}
        if "enabled" in data:
            t.enabled = bool(data["enabled"])
        if "display_name" in data:
            t.display_name = data["display_name"].strip() or t.display_name
        db.commit()
        return jsonify({"ok": True})
    finally:
        db.close()


@app.route("/api/line-targets/<int:tid>", methods=["DELETE"])
def api_line_targets_delete(tid):
    db = SessionLocal()
    try:
        t = db.query(LineTarget).get(tid)
        if t:
            db.delete(t)
            db.commit()
        return jsonify({"ok": True})
    finally:
        db.close()


@app.route("/api/line-targets", methods=["POST"])
def api_line_targets_add():
    """手動新增一個收件對象（管理員自行輸入 ID）"""
    data = request.get_json(force=True, silent=True) or {}
    target_id = (data.get("target_id") or "").strip()
    if not target_id:
        return jsonify({"ok": False, "msg": "target_id 不能空白"}), 400
    db = SessionLocal()
    try:
        if db.query(LineTarget).filter_by(target_id=target_id).first():
            return jsonify({"ok": False, "msg": "已存在"}), 409
        t = LineTarget(
            target_id=target_id,
            target_type=data.get("target_type", "user"),
            display_name=data.get("display_name", "").strip() or None,
            enabled=True,
        )
        db.add(t)
        db.commit()
        return jsonify({"ok": True, "id": t.id})
    finally:
        db.close()


# ─── 啟動 ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    init_db()
    start_scheduler()
    app.run(host="0.0.0.0", port=8080, debug=False)
