# 美股自動交易 Demo

從「設定一個交易條件」→「Alpaca 模擬下單」→「Discord / Telegram 收到通知」。

全程使用 **Alpaca 模擬帳戶(Paper Trading)**,不會動到真實資金。

---

## 一、安裝套件(只需做一次)

```bash
py -m pip install -r requirements.txt
```

---

## 二、拿三組金鑰

### 1. Alpaca 模擬帳戶(必要,約 5 分鐘)

1. 到 https://app.alpaca.markets/ 註冊 / 登入
2. 左上角把模式切到 **Paper**(不是 Live)
3. 右側找到 **API Keys** → **Generate New Key**
4. 記下 `API Key ID` 與 `Secret Key`

> Secret Key 只會顯示一次,關掉就看不到了,要重新產生。

**模擬帳戶不需要入金,也不需要等開戶審核。** 實盤審核是另一件事,跟這支 Demo 無關。

### 2. 通知管道(Discord 與 Telegram 擇一即可)

**Discord(比較快,約 2 分鐘)**

1. 打開你的 Discord 伺服器 → 選一個頻道 → 齒輪(編輯頻道)
2. **整合 → Webhook → 新增 Webhook**
3. 點 **複製 Webhook 網址**

**Telegram(約 5 分鐘)**

1. 在 Telegram 搜尋 `@BotFather`,傳 `/newbot`,照指示取得 **TOKEN**
2. 主動跟你剛建立的 bot 說一句話(隨便打什麼都行)
3. 瀏覽器開 `https://api.telegram.org/bot<你的TOKEN>/getUpdates`
4. 從回傳內容裡找到 `"chat":{"id":數字}`,那串數字就是 **CHAT_ID**

---

## 三、填進 .env

把 `.env.example` 複製一份、改名成 `.env`,填入上面拿到的值:

```bash
cp .env.example .env
```

通知管道只填你要用的那一個,另一個留空即可。

> `.env` 已經寫進 `.gitignore`,不會被上傳到 GitHub。
> **金鑰絕對不要直接寫在程式碼裡。**

---

## 四、設定交易條件

打開 `config.json`:

```json
{
  "symbol": "AAPL",
  "condition": { "type": "below_sma", "period": 5, "value": null },
  "action": { "side": "buy", "qty": 1 },
  "notify_when_no_signal": true
}
```

| 欄位 | 說明 |
|---|---|
| `symbol` | 股票代號,例如 `AAPL`、`TSLA`、`NVDA` |
| `condition.type` | `below_sma`(跌破均線)、`above_sma`(站上均線)、`price_below`(低於某價)、`price_above`(高於某價) |
| `condition.period` | 均線天數,只有 `*_sma` 會用到 |
| `condition.value` | 價格門檻,只有 `price_*` 會用到 |
| `action.side` | `buy` 或 `sell` |
| `action.qty` | 股數 |
| `notify_when_no_signal` | 條件不成立時,要不要也發一則「今日無訊號」 |

---

## 五、執行

```bash
py demo.py
```

其他模式:

| 指令 | 用途 |
|---|---|
| `py demo.py` | 正常執行:條件成立才下單 |
| `py demo.py --force` | 強制觸發,用於錄影示範。畫面與通知都會標明是示範模式 |
| `py demo.py --dry-run` | 只判斷條件與發通知,不真的送出委託 |

---

## 六、關於資料與成交的幾件事(誠實說明)

- 報價來自 **Alpaca 免費方案的 IEX 資料源**,只涵蓋 IEX 交易所的成交,與全市場整合報價(SIP)會有些微差距。付費方案才有 SIP。
- 均線用的是**最近 N 個已收盤交易日**的收盤價,不含今天的未完成 K 棒。
- 台灣時間白天執行時美股是收盤的,市價單會**排到下次開盤才成交**,程式會在畫面與通知裡標明。
- 模擬帳戶的撮合是理想化的,通常立即全額成交,**不模擬部分成交、排隊與流動性不足**。模擬跑得順不等於實盤跑得順。
