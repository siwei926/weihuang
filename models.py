from sqlalchemy import (
    Column, Integer, String, Date, DateTime, Numeric, Boolean, Text,
    ForeignKey, create_engine, text
)
from sqlalchemy.orm import declarative_base, sessionmaker, relationship
import datetime
from config import DATABASE_URL

engine = create_engine(DATABASE_URL, echo=False)
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)
Base = declarative_base()


class ParkingSpot(Base):
    __tablename__ = "parking_spots"

    id = Column(Integer, primary_key=True)
    spot_number = Column(String(20), unique=True, nullable=False)  # e.g. "A1", "B2"
    notes = Column(Text, nullable=True)
    is_active = Column(Boolean, default=True, nullable=False)
    # "rental"=可出租  "disabled"=停用
    spot_type = Column(String(20), nullable=False, default="rental")
    disabled_reason = Column(String(100), nullable=True)  # 停用原因（例：瓜皮使用、施工中…）
    sort_order = Column(Integer, nullable=True)  # 自訂排序；NULL 時依 spot_number

    rentals = relationship("Rental", back_populates="spot", cascade="all, delete-orphan")


class Tenant(Base):
    __tablename__ = "tenants"

    id = Column(Integer, primary_key=True)
    name = Column(String(100), nullable=False)
    # 兼容電話號碼或 Line ID（管理員可擇一填入；作為車主身份識別、需唯一）
    phone = Column(String(100), nullable=True)
    license_plate = Column(String(20), nullable=True)
    notes = Column(Text, nullable=True)
    is_deleted = Column(Boolean, default=False, nullable=False)

    rentals = relationship("Rental", back_populates="tenant", foreign_keys="Rental.tenant_id")


class Rental(Base):
    __tablename__ = "rentals"

    id = Column(Integer, primary_key=True)
    spot_id = Column(Integer, ForeignKey("parking_spots.id"), nullable=False)
    tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False)
    start_date = Column(Date, nullable=False, default=datetime.date.today)
    expected_end_date = Column(Date, nullable=True)   # 預計結束日，到期自動轉空置
    end_date = Column(Date, nullable=True)
    monthly_fee = Column(Numeric(10, 0), nullable=False, default=0)
    status = Column(String(10), nullable=False, default="active")  # active / ended
    notes = Column(Text, nullable=True)

    # 預約換租：下一位車主資訊（start_date 未到前暫存，到期由排程自動執行）
    next_tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=True)
    next_start_date = Column(Date, nullable=True)
    next_expected_end_date = Column(Date, nullable=True)
    next_prepaid_months = Column(Integer, nullable=True, default=1)
    refund_pending = Column(Integer, nullable=True, default=0)  # 預約修改後待退還金額（未確認時暫存）
    original_expected_end_date = Column(Date, nullable=True)  # 建立時的原始結束日；延長合約時不覆寫

    spot = relationship("ParkingSpot", back_populates="rentals")
    tenant = relationship("Tenant", back_populates="rentals", foreign_keys=[tenant_id])
    next_tenant = relationship("Tenant", foreign_keys=[next_tenant_id])
    payments = relationship("Payment", back_populates="rental", cascade="all, delete-orphan")
    # 退費／豁免類紀錄：刪除租約時連帶清除（避免訂單刪除後支出/退費頁面殘留 dangling 紀錄）
    expenses = relationship("Expense", back_populates="rental", cascade="all, delete-orphan")


class Payment(Base):
    __tablename__ = "payments"

    id = Column(Integer, primary_key=True)
    rental_id = Column(Integer, ForeignKey("rentals.id"), nullable=False)
    payment_date = Column(Date, nullable=False, default=datetime.date.today)
    amount = Column(Numeric(10, 0), nullable=False)
    period_month = Column(String(7), nullable=False)  # YYYY-MM
    notes = Column(Text, nullable=True)

    rental = relationship("Rental", back_populates="payments")


class Expense(Base):
    __tablename__ = "expenses"

    id = Column(Integer, primary_key=True)
    expense_date = Column(Date, nullable=False, default=datetime.date.today)
    amount = Column(Numeric(10, 0), nullable=False)
    category = Column(String(30), nullable=False, default="其他")
    description = Column(Text, nullable=True)
    confirmed = Column(Boolean, default=True, nullable=False)  # 退費記錄需手動確認
    created_at = Column(DateTime, default=datetime.datetime.now, nullable=True)
    # 與租約的關聯（退費／豁免類記錄專用）；以 id 識別取代易失準的描述前綴比對
    rental_id = Column(Integer, ForeignKey("rentals.id"), nullable=True)

    rental = relationship("Rental", back_populates="expenses")


class LineTarget(Base):
    """LINE 通知收件對象（個人 or 群組）"""
    __tablename__ = "line_targets"

    id = Column(Integer, primary_key=True)
    target_id = Column(String(100), unique=True, nullable=False)   # UID / groupId
    target_type = Column(String(10), nullable=False, default="user")  # user / group
    display_name = Column(String(100), nullable=True)
    enabled = Column(Boolean, default=True, nullable=False)
    added_at = Column(DateTime, default=datetime.datetime.now, nullable=True)


class AppSetting(Base):
    """通用 key-value 系統設定表（通知排程偏好等）"""
    __tablename__ = "app_settings"

    key = Column(String(100), primary_key=True)
    value = Column(Text, nullable=True)
    updated_at = Column(DateTime, default=datetime.datetime.now, onupdate=datetime.datetime.now, nullable=True)


def init_db():
    Base.metadata.create_all(bind=engine)
    # 遷移：舊資料庫補 spot_type 欄位
    with engine.connect() as conn:
        for sql in [
            "ALTER TABLE parking_spots ADD COLUMN spot_type VARCHAR(20) NOT NULL DEFAULT 'rental'",
            "ALTER TABLE rentals ADD COLUMN expected_end_date DATE",
            "ALTER TABLE tenants ADD COLUMN is_deleted BOOLEAN NOT NULL DEFAULT 0",
            "ALTER TABLE rentals ADD COLUMN next_tenant_id INTEGER REFERENCES tenants(id)",
            "ALTER TABLE rentals ADD COLUMN next_start_date DATE",
            "ALTER TABLE rentals ADD COLUMN next_expected_end_date DATE",
            "ALTER TABLE rentals ADD COLUMN next_prepaid_months INTEGER DEFAULT 1",
            "ALTER TABLE expenses ADD COLUMN confirmed BOOLEAN NOT NULL DEFAULT 1",
            "ALTER TABLE expenses ADD COLUMN created_at DATETIME",
            "ALTER TABLE rentals ADD COLUMN refund_pending INTEGER DEFAULT 0",
            "ALTER TABLE parking_spots ADD COLUMN disabled_reason VARCHAR(100)",
            "ALTER TABLE parking_spots ADD COLUMN sort_order INTEGER",
            "ALTER TABLE expenses ADD COLUMN rental_id INTEGER REFERENCES rentals(id)",
            "ALTER TABLE rentals ADD COLUMN original_expected_end_date DATE",
            # 電話作為車主身份識別 → 部分唯一索引（排除 NULL 與軟刪除車主）
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_tenants_phone_unique ON tenants(phone) WHERE phone IS NOT NULL AND is_deleted = 0",
        ]:
            try:
                conn.execute(text(sql))
                conn.commit()
            except Exception:
                pass  # 欄位已存在
        # 回填舊資料：將現有 original_expected_end_date = NULL 的租約補上 expected_end_date
        try:
            conn.execute(text(
                "UPDATE rentals SET original_expected_end_date = expected_end_date"
                " WHERE original_expected_end_date IS NULL AND expected_end_date IS NOT NULL"
            ))
            conn.commit()
        except Exception:
            pass
    # 預設建立 18 個車位（若資料庫是空的）；並從 config 自動補上主要 LINE 收件人
    from config import LINE_USER_ID
    db = SessionLocal()
    try:
        if db.query(ParkingSpot).count() == 0:
            for i in range(1, 19):
                db.add(ParkingSpot(spot_number=f"{i:02d}"))
            db.commit()
        # 將 config 裡的 LINE_USER_ID 自動新增為第一個收件人（若尚未存在）
        if LINE_USER_ID and not db.query(LineTarget).filter_by(target_id=LINE_USER_ID).first():
            db.add(LineTarget(
                target_id=LINE_USER_ID,
                target_type="user",
                display_name="管理員（預設）",
                enabled=True,
            ))
            db.commit()
    finally:
        db.close()
