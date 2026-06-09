import yfinance as yf
import json
import os
import smtplib
import math
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timedelta, timezone

SYMBOLS = {
    "^N225":      {"name": "日経平均",        "threshold": 1000, "unit": "円"},
    "NKD=F":      {"name": "日経平均先物",    "threshold": 1000, "unit": "円"},
    "USDJPY=X":   {"name": "ドル円",          "threshold": 5,    "unit": "円"},
    "^DJI":       {"name": "ダウ平均",        "threshold": 1000, "unit": "ドル"},
    "^IXIC":      {"name": "NASDAQ総合指数",  "threshold": 500,  "unit": "ポイント"},
    "000001.SS":  {"name": "上海総合指数",    "threshold": 100,  "unit": "ポイント"},
}

# サーキットブレーカー設定
# 上海はCB制度が2016年に停止されているため対象外
CIRCUIT_BREAKERS = {
    "^N225": {
        "name": "日経平均",
        "unit": "円",
        "thresholds": [
            {"pct": 8,  "label": "第1段階（±8%）",  "direction": "both"},
            {"pct": 9,  "label": "第2段階（±9%）",  "direction": "both"},
            {"pct": 10, "label": "第3段階（±10%）", "direction": "both"},
        ],
    },
    "NKD=F": {
        "name": "日経平均先物",
        "unit": "円",
        "thresholds": [
            {"pct": 8,  "label": "第1段階（±8%）",  "direction": "both"},
            {"pct": 9,  "label": "第2段階（±9%）",  "direction": "both"},
            {"pct": 10, "label": "第3段階（±10%）", "direction": "both"},
        ],
    },
    "^DJI": {
        "name": "ダウ平均",
        "unit": "ドル",
        "thresholds": [
            {"pct": 7,  "label": "Level 1（-7%）",        "direction": "down"},
            {"pct": 13, "label": "Level 2（-13%）",       "direction": "down"},
            {"pct": 20, "label": "Level 3（-20%）終日停止", "direction": "down"},
        ],
    },
    "^IXIC": {
        "name": "NASDAQ総合指数",
        "unit": "ポイント",
        "thresholds": [
            {"pct": 7,  "label": "Level 1（-7%）",        "direction": "down"},
            {"pct": 13, "label": "Level 2（-13%）",       "direction": "down"},
            {"pct": 20, "label": "Level 3（-20%）終日停止", "direction": "down"},
        ],
    },
}

# 日中値幅設定
# futures_switch=True の銘柄は現物/先物で基準価格を切り替える
INTRADAY_RANGES = {
    "^N225":    {"name": "日経平均", "unit": "円", "thresholds": [1000, 2000, 3000], "futures_switch": True},
    "USDJPY=X": {"name": "ドル円",   "unit": "円", "thresholds": [1],               "futures_switch": False},
}

STATE_FILE = "state/state.json"
JST = timezone(timedelta(hours=9))

# 東証取引時間（UTC）
# 前場: 9:00-11:30 JST = 0:00-2:30 UTC
# 後場: 12:30-15:30 JST = 3:30-6:30 UTC
def is_tse_open():
    t = datetime.now(timezone.utc)
    m = t.hour * 60 + t.minute
    return (0 <= m < 150) or (210 <= m < 390)


def skip_symbol(symbol):
    if symbol == "^N225" and not is_tse_open():
        return True
    if symbol == "NKD=F" and is_tse_open():
        return True
    return False


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(state):
    os.makedirs("state", exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def get_price(symbol):
    ticker = yf.Ticker(symbol)
    data = ticker.history(period="1d", interval="5m")
    if data.empty:
        return None
    return float(data["Close"].iloc[-1])


def get_prev_close(symbol):
    ticker = yf.Ticker(symbol)
    data = ticker.history(period="5d", interval="1d")
    if len(data) < 2:
        return None
    return float(data["Close"].iloc[-2])


def get_today_hl(symbol):
    ticker = yf.Ticker(symbol)
    data = ticker.history(period="1d", interval="1d")
    if data.empty:
        return None, None
    return float(data["High"].iloc[-1]), float(data["Low"].iloc[-1])


def get_band(price, threshold):
    return math.floor(price / threshold) * threshold


def should_notify(state, key):
    if key not in state:
        return True
    last_time = datetime.fromisoformat(state[key])
    return datetime.now(timezone.utc) - last_time > timedelta(hours=24)


def update_state(state, key):
    state[key] = datetime.now(timezone.utc).isoformat()


def send_email(subject, body):
    gmail_user = os.environ["GMAIL_USER"]
    gmail_password = os.environ["GMAIL_APP_PASSWORD"]
    notify_email = os.environ["NOTIFY_EMAIL"]

    msg = MIMEMultipart()
    msg["From"] = gmail_user
    msg["To"] = notify_email
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain", "utf-8"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(gmail_user, gmail_password)
        server.send_message(msg)


def _fmt_price(price, symbol, config):
    if symbol == "USDJPY=X":
        return f"{price:.2f}{config['unit']}"
    return f"{price:,.0f}{config['unit']}"


def _fmt_delta(delta, symbol, config):
    if symbol == "USDJPY=X":
        return f"前日比{delta:+.2f}{config['unit']}"
    return f"前日比{delta:+,.0f}{config['unit']}"


def check_price_alerts(state):
    """バンド越えアラート: 現在値と前日比を1行に統合して返す"""
    msgs = []
    fetch_errors = []

    for symbol, config in SYMBOLS.items():
        if skip_symbol(symbol):
            print(f"[SKIP] {config['name']}: 時間外")
            continue
        try:
            price = get_price(symbol)
            if price is None:
                print(f"[SKIP] {symbol}: データ取得失敗")
                fetch_errors.append(config["name"])
                continue

            prev_close = get_prev_close(symbol)
            current_band = get_band(price, config["threshold"])
            prev_key = f"band_prev_{symbol}"
            prev_band = state.get(prev_key)

            price_str = _fmt_price(price, symbol, config)
            if prev_close is not None:
                delta = price - prev_close
                tail = f" → {price_str}（{_fmt_delta(delta, symbol, config)}）"
                print(f"[INFO] {config['name']}: {price_str} ({_fmt_delta(delta, symbol, config)})")
            else:
                tail = f" → {price_str}"
                print(f"[INFO] {config['name']}: {price_str}")

            if prev_band is not None and current_band != prev_band:
                direction_up = current_band > prev_band
                step = config["threshold"]

                if direction_up:
                    crossed_levels = range(int(prev_band) + step, int(current_band) + 1, step)
                else:
                    crossed_levels = range(int(prev_band), int(current_band), -step)

                for crossed in crossed_levels:
                    if symbol == "USDJPY=X":
                        verb = "にタッチ" if direction_up else "割れ"
                        msg = f"{config['name']} {crossed:.0f}{config['unit']}{verb}{tail}"
                    else:
                        direction = "上昇" if direction_up else "下落"
                        verb = "を突破" if direction_up else "を割り込む"
                        msg = f"{config['name']} {direction} {crossed:,.0f}{config['unit']}{verb}{tail}"
                    msgs.append(msg)
                    print(f"[ALERT] {msg}")

            state[prev_key] = current_band

        except Exception as e:
            print(f"[ERROR] {symbol}: {e}")
            fetch_errors.append(config["name"])

    return msgs, fetch_errors


def check_circuit_breakers(state):
    msgs = []
    for symbol, config in CIRCUIT_BREAKERS.items():
        if skip_symbol(symbol):
            continue
        try:
            price = get_price(symbol)
            prev_close = get_prev_close(symbol)
            if price is None or prev_close is None:
                continue

            pct = (price - prev_close) / prev_close * 100
            print(f"[INFO] CB check {config['name']}: 前日比{pct:+.2f}%")

            for thresh in config["thresholds"]:
                triggered = (
                    thresh["direction"] == "both" and abs(pct) >= thresh["pct"]
                    or thresh["direction"] == "down" and pct <= -thresh["pct"]
                )
                if triggered:
                    key = f"cb_{symbol}_{thresh['pct']}"
                    if should_notify(state, key):
                        direction = "上昇" if pct > 0 else "下落"
                        msg = (
                            f"【CB警告】{config['name']} {thresh['label']}発動の可能性"
                            f"（前日比{pct:+.1f}% {direction} → {price:,.0f}{config['unit']}）"
                        )
                        update_state(state, key)
                        msgs.append(msg)
                        print(f"[ALERT] {msg}")

        except Exception as e:
            print(f"[ERROR] CB {symbol}: {e}")

    return msgs


def check_intraday_range(state):
    msgs = []
    today = datetime.now(JST).strftime("%Y-%m-%d")
    tse_open = is_tse_open()

    for symbol, config in INTRADAY_RANGES.items():
        try:
            if config.get("futures_switch") and not tse_open:
                futures_sym = "NKD=F"
                base_key = f"futures_base_{futures_sym}_{today}"

                if base_key not in state or not isinstance(state[base_key], (int, float)):
                    base = get_price(futures_sym)
                    if base is None:
                        continue
                    state[base_key] = base
                    print(f"[INFO] 先物基準価格を記録: {base:,.0f}pt")
                    continue

                base = float(state[base_key])
                current = get_price(futures_sym)
                if current is None:
                    continue
                range_val = abs(current - base)
                diff = current - base
                range_desc = f"基準{base:,.0f}→現在{current:,.0f}（{diff:+,.0f}円）"
                session = "先物"
                suffix = "futures"

            else:
                high, low = get_today_hl(symbol)
                if high is None:
                    continue
                range_val = high - low
                range_desc = f"高値{high:,.0f}／安値{low:,.0f}"
                session = "現物" if config.get("futures_switch") else ""
                suffix = "cash"

            label = f"（{session}）" if session else ""
            print(f"[INFO] 値幅 {config['name']}{label}: {range_val:,.0f}{config['unit']}")

            for thresh in config["thresholds"]:
                if range_val >= thresh:
                    key = f"range_{symbol}_{thresh}_{today}_{suffix}"
                    if should_notify(state, key):
                        msg = (
                            f"【値幅】{config['name']}{label} {thresh:,}{config['unit']}超"
                            f"（{range_desc}）"
                        )
                        update_state(state, key)
                        msgs.append(msg)
                        print(f"[ALERT] {msg}")

        except Exception as e:
            print(f"[ERROR] range {symbol}: {e}")

    return msgs


def notify_fetch_errors(state, fetch_errors):
    if not fetch_errors:
        return
    key = f"fetch_error_{datetime.now(JST).strftime('%Y-%m-%d_%H')}"
    if key in state:
        return
    now = datetime.now(JST).strftime("%Y/%m/%d %H:%M")
    body = (
        f"【監視エラー】以下のシンボルのデータ取得に失敗しました。\n\n"
        + "\n".join(f"  ・{e}" for e in fetch_errors)
        + f"\n\n（{now} JST）\n"
        "GitHub Actions の logs/ フォルダを確認してください。"
    )
    subject = f"【監視エラー】データ取得失敗 {len(fetch_errors)}件"
    try:
        send_email(subject, body)
        state[key] = datetime.now(timezone.utc).isoformat()
        print(f"[SENT] エラーメール: {fetch_errors}")
    except Exception as e:
        print(f"[ERROR] エラーメール送信失敗: {e}")


def main():
    state = load_state()

    price_msgs, fetch_errors = check_price_alerts(state)
    cb_msgs = check_circuit_breakers(state)
    range_msgs = check_intraday_range(state)

    notify_fetch_errors(state, fetch_errors)
    save_state(state)

    all_msgs = price_msgs + cb_msgs + range_msgs
    if all_msgs:
        now_str = datetime.now(JST).strftime("%Y/%m/%d %H:%M JST")
        if len(all_msgs) == 1:
            subject = f"【アラート】{all_msgs[0]}"
        else:
            subject = f"【アラート {len(all_msgs)}件】{all_msgs[0]}"
        body = "\n".join(f"・{m}" for m in all_msgs) + f"\n\n{now_str}"
        try:
            send_email(subject, body)
            print(f"[SENT] {subject}")
        except Exception as e:
            print(f"[ERROR] メール送信失敗: {e}")
    else:
        print("[INFO] アラートなし")


if __name__ == "__main__":
    main()
