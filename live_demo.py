# -*- coding: utf-8 -*-
"""
盤中即時示範版
持續輪詢報價 -> 快慢均線交叉 -> Alpaca 模擬下單 -> Discord / Telegram 通知

與 demo.py 的差別:
  demo.py      跑一次就結束(課程 Part 1 的正式版本)
  live_demo.py 持續執行,用於現場演示

設計重點:
  1. 均線由程式自己輪詢累積,不依賴有延遲的歷史資料
  2. 空手才找買進訊號,有持倉才找賣出訊號 -> 同一個訊號不會重複下單
  3. 只有實際成交才發通知,不是每次判斷都發

用法:
    py live_demo.py
    py live_demo.py --minutes 30
    py live_demo.py --dry-run
"""
import sys, os, json, time, argparse
from collections import deque
from datetime import datetime
from zoneinfo import ZoneInfo

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import requests
from dotenv import load_dotenv

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.data.historical import StockHistoricalDataClient, CryptoHistoricalDataClient
from alpaca.data.requests import (StockLatestTradeRequest, CryptoLatestTradeRequest,
                                  CryptoLatestQuoteRequest)
from alpaca.data.enums import DataFeed

TPE = ZoneInfo("Asia/Taipei")
HERE = os.path.dirname(os.path.abspath(__file__))
W = 68


def line(ch="-"):
    print(ch * W)


def die(msg, hint=""):
    print("\n[停止] {}".format(msg))
    if hint:
        print("       {}".format(hint))
    sys.exit(1)


def fmt(p):
    """價格顯示:BTC 七萬多、DOGE 零點零八,小數位數要跟著變。"""
    if p >= 100:
        return "{:,.2f}".format(p)
    if p >= 1:
        return "{:.3f}".format(p)
    return "{:.5f}".format(p)


# ---- 通知 ------------------------------------------------------------
def send_discord(url, title, color, fields, footer):
    payload = {"embeds": [{
        "title": title, "color": color,
        "fields": [{"name": k, "value": str(v), "inline": inl} for k, v, inl in fields],
        "footer": {"text": footer},
    }]}
    r = requests.post(url, json=payload, timeout=15)
    return r.status_code in (200, 204)


def send_telegram(token, chat_id, title, fields, footer):
    body = "\n".join("{}:{}".format(k, v) for k, v, _ in fields)
    text = "*{}*\n\n{}\n\n_{}_".format(title, body, footer)
    r = requests.post("https://api.telegram.org/bot{}/sendMessage".format(token),
                      json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
                      timeout=15)
    return r.status_code == 200


def notify(title, color, fields, footer):
    hook = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    sent = []
    try:
        if hook and send_discord(hook, title, color, fields, footer):
            sent.append("Discord")
        if token and chat and send_telegram(token, chat, title, fields, footer):
            sent.append("Telegram")
    except Exception as e:
        print("   通知發送失敗:{}".format(e))
    return sent


# ---- 下單 ------------------------------------------------------------
def is_crypto(symbol):
    """加密貨幣代號含斜線,例如 BTC/USD。股票沒有。"""
    return "/" in symbol


def fetch_price(clients, symbol):
    """依標的類型取得參考價。

    加密貨幣用買賣報價的中間價:成交筆數稀疏時,最新成交價會連續重複好幾次,
    均線會遲鈍;中間價則持續更新,訊號比較即時。
    """
    if is_crypto(symbol):
        c = clients["crypto"]
        try:
            q = c.get_crypto_latest_quote(
                CryptoLatestQuoteRequest(symbol_or_symbols=symbol))[symbol]
            bid, ask = float(q.bid_price), float(q.ask_price)
            if bid > 0 and ask > 0:
                return (bid + ask) / 2
        except Exception:
            pass
        return float(c.get_crypto_latest_trade(
            CryptoLatestTradeRequest(symbol_or_symbols=symbol))[symbol].price)
    s = clients["stock"]
    return float(s.get_stock_latest_trade(
        StockLatestTradeRequest(symbol_or_symbols=symbol, feed=DataFeed.IEX))[symbol].price)


def submit(trading, symbol, qty, side, dry_run):
    if dry_run:
        return "(dry-run)", "未送出", None
    # 加密貨幣不接受 DAY,必須用 GTC
    tif = TimeInForce.GTC if is_crypto(symbol) else TimeInForce.DAY
    order = trading.submit_order(MarketOrderRequest(
        symbol=symbol, qty=qty, side=side, time_in_force=tif))
    oid = str(order.id)
    fill = None
    status = str(order.status.value)
    for _ in range(10):
        time.sleep(0.6)
        try:
            o = trading.get_order_by_id(oid)
        except Exception:
            break
        status = str(o.status.value)
        if o.filled_avg_price:
            fill = float(o.filled_avg_price)
        if status in ("filled", "canceled", "rejected", "expired"):
            break
    return oid, status, fill


def current_position(trading, symbol):
    for s in (symbol, symbol.replace("/", "")):
        try:
            return float(trading.get_open_position(s).qty)
        except Exception:
            continue
    return 0.0


# ---- 主程式 ----------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=int, default=None, help="執行幾分鐘")
    ap.add_argument("--symbol", type=str, default=None, help="覆寫股票代號")
    ap.add_argument("--qty", type=float, default=None, help="覆寫每筆數量")
    ap.add_argument("--dry-run", action="store_true", help="只判斷不下單")
    args = ap.parse_args()

    load_dotenv(os.path.join(HERE, ".env"))
    key = os.getenv("ALPACA_API_KEY", "").strip()
    secret = os.getenv("ALPACA_SECRET_KEY", "").strip()
    if not key or not secret or key.startswith("你的"):
        die("找不到 Alpaca 金鑰。", "確認 .env 已填好 Paper 帳戶的 API Key 與 Secret。")

    with open(os.path.join(HERE, "live_config.json"), encoding="utf-8") as f:
        cfg = json.load(f)

    sym = args.symbol if args.symbol else cfg["symbol"]
    qty = args.qty if args.qty else cfg["qty"]
    fast_n = cfg["fast_period"]
    slow_n = cfg["slow_period"]
    poll = cfg["poll_seconds"]
    run_min = args.minutes if args.minutes else cfg["run_minutes"]
    max_trades = cfg["max_trades"]
    min_gap = cfg.get("min_gap_seconds", 180)
    margin_pct = cfg.get("cross_margin_pct", 0.02)

    crypto = is_crypto(sym)
    # 股票說「股」,加密貨幣說幣別本身(0.005 BTC)
    qty_zh = "{:g} {}".format(qty, sym.split("/")[0]) if crypto else "{:g} 股".format(qty)

    trading = TradingClient(key, secret, paper=True)
    clients = {"stock": StockHistoricalDataClient(key, secret),
               "crypto": CryptoHistoricalDataClient(key, secret)}

    try:
        acct = trading.get_account()
        clock = trading.get_clock()
    except Exception as e:
        die("連線 Alpaca 失敗:{}".format(e), "確認金鑰是 Paper 那一組。")

    print()
    line("=")
    print("  盤中即時示範｜均線交叉 -> 模擬下單 -> 手機通知")
    line("=")
    print("  標的      :{}".format(sym))
    print("  策略      :{} 期均線 上穿/下穿 {} 期均線".format(fast_n, slow_n))
    print("  每筆數量  :{}".format(qty_zh))
    print("  輪詢間隔  :{} 秒".format(poll))
    print("  訊號門檻  :快慢線差距需超過 {}%,才算有效交叉(濾掉均線糾纏)".format(margin_pct))
    print("  交易冷卻  :兩筆交易至少間隔 {} 秒".format(min_gap))
    print("  執行時間  :{} 分鐘   交易上限:{} 筆".format(run_min, max_trades))
    print("  帳戶      :Alpaca 模擬帳戶(Paper),可用資金 {:,.2f} 美元".format(float(acct.cash)))
    print()
    print("  註:為了現場演示,已把輪詢頻率壓到 {} 秒一次。".format(poll))
    print("     課程正式版本是收盤後決策,或盤中 15-30 分鐘一次。")
    line("=")

    if crypto:
        print()
        print("  標的為加密貨幣,24 小時交易,不受美股開收盤限制。")
    elif not clock.is_open:
        print()
        print("  目前美股已收盤,市價單會排到下次開盤才成交,")
        print("  現場演示看不到即時成交。下次開盤:{:%m/%d %H:%M}(台北)".format(
            clock.next_open.astimezone(TPE)))
        print("  想在非交易時段演示,把 symbol 改成 BTC/USD 這類加密貨幣即可。")
        print()
        if not sys.stdin.isatty():
            # 在 GitHub Actions 這類非互動環境:收盤就正常結束,不算失敗
            print("  非互動環境,收盤不執行。本次結束。")
            return
        ans = input("  還是要繼續執行嗎?(y/N) ").strip().lower()
        if ans != "y":
            return

    # 起始持倉
    held = current_position(trading, sym)
    state = "long" if held > 0 else "flat"
    print()
    if held > 0:
        print("  起始狀態:已持有 {} 股 {},先等賣出訊號。".format(held, sym))
    else:
        print("  起始狀態:空手,等待買進訊號。")

    # 暖機:先累積足夠的報價點
    prices = deque(maxlen=slow_n)
    print("  暖機中:先累積 {} 個報價點才開始判斷...".format(slow_n))
    print()

    t_end = time.time() + run_min * 60
    trades = 0
    regime = None          # "up" / "down" / None,套用門檻後的多空判定
    last_trade_ts = 0.0
    entry_price = None     # 目前持倉的進場均價
    realized_pl = 0.0      # 本次演示的累計已實現損益

    while time.time() < t_end and trades < max_trades:
        now = datetime.now(TPE)
        try:
            price = fetch_price(clients, sym)
        except Exception as e:
            print("  [{:%H:%M:%S}] 取價失敗,{} 秒後重試:{}".format(now, poll, str(e)[:80]))
            time.sleep(poll)
            continue

        prices.append(price)

        if len(prices) < slow_n:
            print("  [{:%H:%M:%S}] {}  {}  │ 暖機 {}/{}".format(
                now, sym, fmt(price), len(prices), slow_n))
            time.sleep(poll)
            continue

        pl = list(prices)
        fast = sum(pl[-fast_n:]) / fast_n
        slow = sum(pl) / slow_n
        diff = fast - slow
        margin = price * margin_pct / 100.0

        # 套用門檻:差距落在 ±margin 之內視為糾纏,維持前一個判定不變
        prev_regime = regime
        if diff > margin:
            regime = "up"
        elif diff < -margin:
            regime = "down"
        cross_up = regime == "up" and prev_regime != "up"
        cross_dn = regime == "down" and prev_regime != "down"

        state_zh = "持倉" if state == "long" else "空手"
        wait_zh = "等賣出訊號" if state == "long" else "等買進訊號"
        mark = ""
        if cross_up:
            mark = "  ^ 快線有效上穿"
        elif cross_dn:
            mark = "  v 快線有效下穿"
        elif regime is None:
            mark = "  (糾纏中)"
        print("  [{:%H:%M:%S}] {}  {}  │ 快線 {}  慢線 {}  差 {:+.4f}  │ {}  {}{}".format(
            now, sym, fmt(price), fmt(fast), fmt(slow), diff, state_zh, wait_zh, mark))

        # 冷卻期:訊號成立但距上一筆太近,不下單(防止同一波行情反覆進出)
        if (cross_up or cross_dn) and (time.time() - last_trade_ts) < min_gap:
            wait_s = int(min_gap - (time.time() - last_trade_ts))
            print("        訊號成立,但距上一筆交易未滿 {} 秒,略過(還需 {} 秒)".format(
                min_gap, wait_s))
            time.sleep(poll)
            continue

        fire = None
        if cross_up and state == "flat":
            fire = ("buy", OrderSide.BUY, "買進", 0x2ECC71,
                    "短期均價站上長期均價,走勢轉強\n({} 期 {} 高於 {} 期 {})".format(
                        fast_n, fmt(fast), slow_n, fmt(slow)))
        elif cross_dn and state == "long":
            fire = ("sell", OrderSide.SELL, "賣出", 0xE74C3C,
                    "短期均價跌破長期均價,走勢轉弱\n({} 期 {} 低於 {} 期 {})".format(
                        fast_n, fmt(fast), slow_n, fmt(slow)))

        if fire:
            _, side, side_zh, color, reason = fire
            print()
            print("   +--- 觸發{} ".format(side_zh) + "-" * (W - 16))
            print("   | 送出市價{}單  {}  {}".format(side_zh, sym, qty_zh))
            try:
                oid, status, fill = submit(trading, sym, qty, side, args.dry_run)
            except Exception as e:
                print("   | 下單失敗:{}".format(str(e)[:120]))
                print("   +" + "-" * (W - 4))
                print()
                time.sleep(poll)
                continue
            ref = fill if fill else price
            fill_zh = "{} 美元".format(fmt(fill)) if fill else "尚未回報({} 參考價)".format(fmt(price))
            print("   | 訂單編號  {}".format(oid))
            print("   | 委託狀態  {}   成交均價 {}".format(status, fill_zh))

            # ---- 損益計算 ----
            if side == OrderSide.BUY:
                entry_price = ref
                cost = ref * qty
                print("   | 投入金額  {:,.2f} 美元".format(cost))
                fields = [
                    ("標的", sym, True),
                    ("動作", "買進 {}".format(qty_zh), True),
                    ("觸發原因", reason, False),
                    ("成交均價", "{} 美元".format(fmt(ref)), True),
                    ("投入金額", "{:,.2f} 美元".format(cost), True),
                    ("本次演示累計已實現損益", "{:+,.2f} 美元".format(realized_pl), False),
                    ("訂單編號", oid, False),
                ]
            else:
                trade_pl = (ref - entry_price) * qty if entry_price else 0.0
                trade_pct = ((ref / entry_price - 1) * 100) if entry_price else 0.0
                realized_pl += trade_pl
                color = 0x2ECC71 if trade_pl >= 0 else 0xE74C3C
                tag = "獲利" if trade_pl >= 0 else "虧損"
                print("   | 進場均價  {}   出場均價  {}".format(fmt(entry_price or 0), fmt(ref)))
                print("   | 本筆{}  {:+,.2f} 美元 ({:+.2f}%)".format(tag, trade_pl, trade_pct))
                print("   | 累計損益  {:+,.2f} 美元".format(realized_pl))
                fields = [
                    ("標的", sym, True),
                    ("動作", "賣出 {}".format(qty_zh), True),
                    ("觸發原因", reason, False),
                    ("進場均價", "{} 美元".format(fmt(entry_price or 0)), True),
                    ("出場均價", "{} 美元".format(fmt(ref)), True),
                    ("本筆{}".format(tag), "{:+,.2f} 美元 ({:+.2f}%)".format(trade_pl, trade_pct), False),
                    ("本次演示累計已實現損益", "{:+,.2f} 美元".format(realized_pl), False),
                    ("訂單編號", oid, False),
                ]
                entry_price = None

            if args.dry_run:
                print("   | [dry-run] 略過通知")
                print("   +" + "-" * (W - 4))
                print()
                state = "long" if side == OrderSide.BUY else "flat"
                trades += 1
                last_trade_ts = time.time()
                time.sleep(poll)
                continue
            sent = notify(
                "已送出模擬{}單".format(side_zh), color, fields,
                "{:%Y-%m-%d %H:%M:%S} 台北｜Alpaca 模擬帳戶,未使用真實資金".format(now))
            print("   | 通知已送出 -> {}".format(" 、 ".join(sent) if sent else "(未設定通知管道)"))
            print("   +" + "-" * (W - 4))
            print()
            state = "long" if side == OrderSide.BUY else "flat"
            trades += 1
            last_trade_ts = time.time()

        time.sleep(poll)

    print()
    line("=")
    reason_end = "達到交易筆數上限" if trades >= max_trades else "時間到"
    print("  演示結束({})。這段期間共送出 {} 筆模擬委託。".format(reason_end, trades))
    print("  累計已實現損益:{:+,.2f} 美元".format(realized_pl))
    if state == "long":
        print("  目前狀態:持有 {}(尚未平倉,損益未計入)".format(qty_zh))
    else:
        print("  目前狀態:空手")
    try:
        eq = float(trading.get_account().equity)
        print("  帳戶總值:{:,.2f} 美元(模擬資金)".format(eq))
    except Exception:
        pass
    line("=")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n  已手動中止。")
