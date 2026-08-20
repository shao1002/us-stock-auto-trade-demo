# -*- coding: utf-8 -*-
"""
美股量化課程 Demo
從「設定一個交易條件」→「Alpaca 模擬下單」→「Discord / Telegram 收到通知」

用法:
    py demo.py              # 正常執行:檢查條件,成立才下單
    py demo.py --force      # 強制觸發(錄影示範用,畫面與通知都會標明)
    py demo.py --dry-run    # 只檢查條件與發通知,不真的下單
"""
import sys, os, json, argparse
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import requests
from dotenv import load_dotenv

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestTradeRequest, StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.data.enums import DataFeed

TPE = ZoneInfo("Asia/Taipei")
NY = ZoneInfo("America/New_York")
HERE = os.path.dirname(os.path.abspath(__file__))

W = 62


def line(ch="-"):
    print(ch * W)


def step(n, title):
    print()
    line()
    print("  步驟 {}｜{}".format(n, title))
    line()


def die(msg, hint=""):
    print("\n[停止] {}".format(msg))
    if hint:
        print("       {}".format(hint))
    sys.exit(1)


# ---- 把設定檔翻譯成人話 ----------------------------------------------
def describe(cfg):
    sym = cfg["symbol"]
    c = cfg["condition"]
    a = cfg["action"]
    side = "買進" if a["side"].lower() == "buy" else "賣出"
    t = c["type"]
    if t == "below_sma":
        cond = "{} 現價 低於 {} 日均線".format(sym, c["period"])
    elif t == "above_sma":
        cond = "{} 現價 高於 {} 日均線".format(sym, c["period"])
    elif t == "price_below":
        cond = "{} 現價 低於 {} 美元".format(sym, c["value"])
    elif t == "price_above":
        cond = "{} 現價 高於 {} 美元".format(sym, c["value"])
    else:
        die("config.json 裡不認得的條件類型:{}".format(t),
            "可用:below_sma / above_sma / price_below / price_above")
    return "如果「{}」,就{} {} 股".format(cond, side, a["qty"])


# ---- 取價與均線 ------------------------------------------------------
def get_price(data_client, symbol):
    req = StockLatestTradeRequest(symbol_or_symbols=symbol, feed=DataFeed.IEX)
    trade = data_client.get_stock_latest_trade(req)[symbol]
    return float(trade.price), trade.timestamp


def get_sma(data_client, symbol, period):
    end = datetime.now(timezone.utc) - timedelta(minutes=20)
    start = end - timedelta(days=period * 4 + 20)
    req = StockBarsRequest(symbol_or_symbols=symbol, timeframe=TimeFrame.Day,
                           start=start, end=end, feed=DataFeed.IEX)
    bars = data_client.get_stock_bars(req).data.get(symbol, [])
    today_ny = datetime.now(NY).date()
    closed = [b for b in bars if b.timestamp.astimezone(NY).date() < today_ny]
    if len(closed) < period:
        die("{} 的日線資料不足({}/{} 根)".format(symbol, len(closed), period),
            "把 config.json 的 period 調小,或改用 price_below 這類固定價格條件。")
    use = closed[-period:]
    sma = sum(float(b.close) for b in use) / period
    return sma, use


def evaluate(cfg, data_client):
    sym = cfg["symbol"]
    c = cfg["condition"]
    price, ts = get_price(data_client, sym)
    print("  {} 目前成交價:{:.2f} 美元".format(sym, price))
    print("  報價時間      :{:%Y-%m-%d %H:%M:%S}(台北)".format(ts.astimezone(TPE)))
    print("  資料來源      :Alpaca IEX(免費方案)")

    t = c["type"]
    if t in ("below_sma", "above_sma"):
        sma, used = get_sma(data_client, sym, c["period"])
        days = ",  ".join("{:%m/%d} {:.2f}".format(b.timestamp.astimezone(NY), float(b.close))
                          for b in used)
        print()
        print("  取最近 {} 個交易日收盤價:".format(c["period"]))
        print("    {}".format(days))
        print("  {} 日均線     :{:.2f} 美元".format(c["period"], sma))
        hit = price < sma if t == "below_sma" else price > sma
        word = "低於" if t == "below_sma" else "高於"
        detail = "現價 {:.2f}  {}  {}日均線 {:.2f}  →  條件{}".format(
            price, "<" if price < sma else ">=", c["period"], sma,
            "成立" if hit else "不成立")
        reason = "現價 {:.2f} {} {} 日均線 {:.2f}".format(price, word, c["period"], sma)
    else:
        target = float(c["value"])
        hit = price < target if t == "price_below" else price > target
        word = "低於" if t == "price_below" else "高於"
        detail = "現價 {:.2f}  vs  門檻 {:.2f}  →  條件{}".format(
            price, target, "成立" if hit else "不成立")
        reason = "現價 {:.2f} {} 設定門檻 {:.2f}".format(price, word, target)
    print()
    print("  判斷:{}".format(detail))
    return hit, price, reason


# ---- 通知 ------------------------------------------------------------
def send_discord(url, title, color, fields, footer):
    payload = {"embeds": [{
        "title": title,
        "color": color,
        "fields": [{"name": k, "value": str(v), "inline": inl} for k, v, inl in fields],
        "footer": {"text": footer},
    }]}
    r = requests.post(url, json=payload, timeout=15)
    if r.status_code not in (200, 204):
        print("  [通知失敗] Discord 回應 {}:{}".format(r.status_code, r.text[:200]))
        return False
    return True


def send_telegram(token, chat_id, title, fields, footer):
    body = "\n".join("{}:{}".format(k, v) for k, v, _ in fields)
    text = "*{}*\n\n{}\n\n_{}_".format(title, body, footer)
    r = requests.post("https://api.telegram.org/bot{}/sendMessage".format(token),
                      json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
                      timeout=15)
    if r.status_code != 200:
        print("  [通知失敗] Telegram 回應 {}:{}".format(r.status_code, r.text[:200]))
        return False
    return True


def notify(title, color, fields, footer):
    hook = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    sent = []
    if hook and send_discord(hook, title, color, fields, footer):
        sent.append("Discord")
    if token and chat and send_telegram(token, chat, title, fields, footer):
        sent.append("Telegram")
    if not sent:
        print("  [略過] .env 裡沒有填任何通知管道,只在畫面顯示。")
    else:
        print("  已送出通知 → {}".format(" 、 ".join(sent)))
    return sent


# ---- 主流程 ----------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="略過條件檢查,強制下單(示範用)")
    ap.add_argument("--dry-run", action="store_true", help="不真的下單")
    args = ap.parse_args()

    load_dotenv(os.path.join(HERE, ".env"))
    key = os.getenv("ALPACA_API_KEY", "").strip()
    secret = os.getenv("ALPACA_SECRET_KEY", "").strip()
    if not key or not secret or key.startswith("你的"):
        die("找不到 Alpaca 金鑰。",
            "把 .env.example 複製成 .env,填入 Paper 帳戶的 API Key 與 Secret。")

    with open(os.path.join(HERE, "config.json"), encoding="utf-8") as f:
        cfg = json.load(f)

    print()
    line("=")
    print("  美股自動交易 Demo｜條件判斷 → 模擬下單 → 手機通知")
    print("  執行時間:{:%Y-%m-%d %H:%M:%S}(台北)".format(datetime.now(TPE)))
    line("=")

    step(1, "讀取你設定的交易條件")
    print("  設定檔:config.json")
    print()
    print("  {}".format(describe(cfg)))

    trading = TradingClient(key, secret, paper=True)
    data = StockHistoricalDataClient(key, secret)

    try:
        acct = trading.get_account()
        clock = trading.get_clock()
    except Exception as e:
        die("連線 Alpaca 失敗:{}".format(e),
            "確認 .env 的金鑰是「Paper Trading」那組,不是實盤那組。")

    print()
    print("  帳戶類型:模擬帳戶(Paper)")
    print("  可用資金:{:,.2f} 美元".format(float(acct.cash)))
    print("  市場狀態:{}".format("開盤中" if clock.is_open else "已收盤"))
    if not clock.is_open:
        print("  下次開盤:{:%m/%d %H:%M}(台北)".format(clock.next_open.astimezone(TPE)))

    step(2, "抓取報價,判斷條件是否成立")
    hit, price, reason = evaluate(cfg, data)

    if args.force and not hit:
        print()
        print("  [示範模式] 條件未成立,但因為加了 --force 而強制往下執行。")
        hit = True
        reason += "(示範模式強制觸發)"

    step(3, "條件成立才下單" if hit else "條件不成立,不下單")

    if not hit:
        print("  結果:今天不進場。")
        print()
        print("  這一步是重點:系統每天都會跑,但只有條件成立才會動作。")
        if cfg.get("notify_when_no_signal"):
            print()
            notify("今日無進場訊號", 0x95A5A6,
                   [("標的", cfg["symbol"], True),
                    ("現價", "{:.2f} 美元".format(price), True),
                    ("判斷", reason, False)],
                   "{:%Y-%m-%d %H:%M} 台北｜Alpaca 模擬帳戶".format(datetime.now(TPE)))
        print()
        line("=")
        print("  Demo 結束:條件未成立,系統正確地選擇不動作。")
        line("=")
        return

    a = cfg["action"]
    side = OrderSide.BUY if a["side"].lower() == "buy" else OrderSide.SELL
    side_zh = "買進" if side == OrderSide.BUY else "賣出"
    print("  條件成立 → 準備{} {} {} 股".format(side_zh, cfg["symbol"], a["qty"]))

    if args.dry_run:
        print()
        print("  [dry-run] 不送出委託。")
        order_id, status = "(dry-run 未下單)", "未送出"
    else:
        try:
            order = trading.submit_order(MarketOrderRequest(
                symbol=cfg["symbol"], qty=a["qty"], side=side,
                time_in_force=TimeInForce.DAY))
        except Exception as e:
            die("下單失敗:{}".format(e),
                "常見原因:資金不足、股票代號錯誤、或該檔目前不可交易。")
        order_id = str(order.id)
        status = str(order.status.value)
        print()
        print("  已送出委託")
        print("  訂單編號:{}".format(order_id))
        print("  委託狀態:{}".format(status))
        if not clock.is_open:
            print("  註:目前收盤,這筆市價單會排到下次開盤才成交。")

    step(4, "發送通知到手機")
    fill_note = "市價單,即時成交" if clock.is_open else "市價單,開盤後成交"
    fields = [
        ("標的", cfg["symbol"], True),
        ("動作", "{} {} 股".format(side_zh, a["qty"]), True),
        ("觸發原因", reason, False),
        ("委託方式", fill_note, True),
        ("訂單編號", order_id, False),
    ]
    notify("已送出模擬委託", 0x2ECC71, fields,
           "{:%Y-%m-%d %H:%M} 台北｜Alpaca 模擬帳戶,未使用真實資金".format(datetime.now(TPE)))

    print()
    line("=")
    print("  Demo 結束:條件成立 → 已下單 → 通知已送出。")
    line("=")


if __name__ == "__main__":
    main()
