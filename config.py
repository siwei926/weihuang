import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
CONTRACTS_DIR = os.path.join(DATA_DIR, "contracts")
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(CONTRACTS_DIR, exist_ok=True)

DATABASE_URL = f"sqlite:///{os.path.join(DATA_DIR, 'parking.db')}"

SECRET_KEY = os.environ.get("SECRET_KEY", "parking-secret-change-me")

# Line Messaging API
# 申請步驟：https://developers.line.biz/ → 建立 Messaging API Channel
# 取得 Channel Access Token 與自己的 User ID
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
LINE_USER_ID = os.environ.get("LINE_USER_ID", "")  # 或 LINE_GROUP_ID

# 提前幾天發送未繳費通知（每月幾號後算未繳）
PAYMENT_DUE_DAY = int(os.environ.get("PAYMENT_DUE_DAY", "5"))   # 每月5號前應繳費
PAYMENT_NOTIFY_DAYS_BEFORE = int(os.environ.get("PAYMENT_NOTIFY_DAYS_BEFORE", "3"))
