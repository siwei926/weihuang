# 停車場租賃管理系統

家裡有一個小型停車場，原本用 Excel 記帳，時間久了越來越難追蹤哪個車位繳了、哪個快到期、退了多少錢。就乾脆自己寫一套。

目前跑在家裡的 Synology NAS 上，用 Docker 部署，只在內網使用。

---

## 主要功能

**儀表板**
一眼看出全場狀況。綠色空置、藍色已繳、紅色未繳、黃色快到期，點任一車位直接操作。

**出租 & 繳費**
記錄每筆租約和繳費，支援提前多月繳、中途縮短退款、延長合約補差額。備註欄會自動顯示金額調整的計算過程，比如 `12,000 － 1,000 ＝ 11,000 元`，方便對帳。

**合約照片**
每筆租約可以上傳合約掃描照，方便查找。

**預約換手**
租約到期前先設定好下一位車主，到期當天排程自動建立新租約，不用手動處理。

**LINE 通知**
每天早上自動推送：未繳費清單、快到期提醒（30 / 7 / 1 天前各一次）、待退款提醒、每月收支摘要。支援多人接收，家人也可以一起收到通知。

**自動備份**
每小時整點備份一次，保留策略分四層（近 24 小時 / 近 7 天 / 近 12 個月 / 近 10 年），最多約 53 個檔，可從介面還原任意時間點。

**財務報表**
按月統計收入、支出、淨利，大概知道停車場的盈虧狀況。

---

## 技術

Python / Flask / SQLite / Bootstrap 5 / LINE Messaging API / Docker

---

## 部署

```bash
cp .env.example .env
# 填入 LINE Token 等設定

docker compose up -d --build
```

需要設定的環境變數在 `.env.example` 裡有說明。

<img width="3797" height="1327" alt="image" src="https://github.com/user-attachments/assets/49dfb3d9-d6ac-4947-ab76-2dcce2072493" />
<img width="3784" height="1453" alt="image" src="https://github.com/user-attachments/assets/84b98b80-e99e-4d76-afa7-9e08fc4ebb69" />
<img width="3724" height="1694" alt="image" src="https://github.com/user-attachments/assets/67f3bd84-fb59-4988-aa24-7e62396282f9" />


