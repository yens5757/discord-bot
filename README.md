# 收網機械人

![幸運轉盤](spin_preview.gif)

## 功能一覽

### 自動回覆
- bot 會回覆「收網」

### 娛樂
- `/duck`：回覆「收到風鵝鵝殺」

### 10mix 排隊
- `/join`：加入排隊，滿 10 人自動開賽，按 ELO 分成最平均的兩隊
- `/leave`：離開排隊
- `/queue`：查看排隊名單

### 記錄結果
- `/result 10mix 編號 勝方`：按兩隊實力差距加減 ELO（實力相同 ±20）
- `/result flex p1~p5 贏/輸`：記錄 5 人 Flex 結果，每人 ±20
- `/cancel 編號`：取消比賽，不計 ELO

### 統計
- `/stat [玩家]`：個人戰績卡（ELO、金幣、10mix 及 Flex 勝負與勝率）
- `/leaderboard`：ELO 前 10 名
- `/duo 最強/炸裂 [玩家]`：和某人同隊時勝率最高／最低的隊友
- `/lineup 最強/炸裂`：勝率最高／最低的 5 人陣容

### 金幣與卡面
- 每人起始 100 金幣
- 每場比賽：贏 +10 金幣，輸 +3 金幣
- `/shop`：查看 6 款戰績卡及價錢
- `/buy 款式`：購買並套用卡面
- `/style 款式`：更換已擁有的卡面

### 幸運轉盤
- `/spin`：每日 1 次免費（香港時間 00:00 重置），之後每次 25 金幣
- 獎品：收網、10／25／50／100 金幣、再轉一次、250 金幣 Jackpot、限定卡面「彩虹」

### 管理員
- `/setelo 玩家 數值`：直接修改 ELO

## 安裝及運行

1. 安裝 Python 3.10 或以上，然後安裝套件：
   ```
   pip install -r requirements.txt
   ```
2. 喺專案資料夾建立 `.env` 檔案，填入 Discord bot token：
   ```
   DISCORD_TOKEN=你的token
   ```
3. 運行：
   ```
   python bot.py
   ```

戰績卡需要中文字型：Windows 會用微軟正黑體，macOS 用蘋方，Linux 需要安裝 Noto Sans CJK。
