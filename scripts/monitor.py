import yfinance as yf
import json
import os
import smtplib
import math
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timedelta, timezone

SYMBOLS = {
    "^N225":    {"name": "日経平均",     "threshold": 1000, "unit": "円"},
    "NKD=F":    {"name": "日経平均先物", "threshold": 1000, "unit": "pt"},
    "USDJPY=X": {"name": "ドル円",       "threshold": 5,    "unit": "円"},
    "^GSPC":    {"name": "S&P500",      "threshold": 500,  "unit": "ドル"},
}

# サーキットブレーカー設定
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
        "unit": "pt",
        "thresholds": [
            {"pct": 8,  "label": "第1段階（±8%）",  "direction": "both"},
            {"pct": 9,  "label": "第2段階（±9%）",  "direction": "both"},
            {"pct": 10, "label": "第3段階（±10%）", "direction": "both"},
        ],
    },
    "^GSPC": {
        "name": "S&P500",
        "unit": "ドル",
        "thresholds": [
            {"pct": 7,  "label": "Level 1（-7%）",        "direction": "down"},
            {"pct": 13, "label": "Level 2（-13%）",       "direction": "down"},
            {"pct": 20, "label": "Level 3（-20%）終日停止", "direction": "down"},
        ],
    },
}

# 大台設定（10,000円単位のキリ番を動的に検出）
MILESTONES = {
    "^N225": {"name": "日経平均", "unit": "円", "threshold": 10000},
}

# 日中値幅設定（日経平均）
INTRADAY_RANGES = {
    "^N225": {"name": "日経平均", "unit": "円", "thresholds": [1000, 2000, 3000]},
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


def check_price_alerts(state):
    notified = False
    for symbol, config in SYMBOLS.items():
        if skip_symbol(symbol):
            print(f"[SKIP] {config['name']}: 時間外")
            continue
        try:
            price = get_price(symbol)
            if price is None:
                print(f"[SKIP] {symbol}: データ取得失敗")
                continue

            band = get_band(price, config["threshold"])
            print(f"[INFO] {config['name']}: {price:,.2f} (band={band:,.0f})")

            key = f"{symbol}_{band}"
            if should_notify(state, key):
                now = datetime.now(JST).strftime("%Y/%m/%d %H:%M")
                direction = "上抜け" if price >= band + config["threshold"] / 2 else "下抜け"
                body = (
                    f"【{config['name']}アラート】"
                    f"{band:,.0f}{config['unit']}を{direction}。"
                    f"現在値：{price:,.2f}{config['unit']}（{now} JST）"
                )
                subject = f"【価格アラート】{config['name']} {band:,.0f}{config['unit']}帯"
                send_email(subject, body)
                update_state(state, key)
                print(f"[SENT] {body}")
                notified = True

        except Exception as e:
            print(f"[ERROR] {symbol}: {e}")

    return notified


def check_circuit_breakers(state):
    notified = False
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
                triggered = False
                if thresh["direction"] == "both" and abs(pct) >= thresh["pct"]:
                    triggered = True
                elif thresh["direction"] == "down" and pct <= -thresh["pct"]:
                    triggered = True

                if triggered:
                    key = f"cb_{symbol}_{thresh['pct']}"
                    if should_notify(state, key):
                        now = datetime.now(JST).strftime("%Y/%m/%d %H:%M")
                        direction = "上昇" if pct > 0 else "下落"
                        body = (
                            f"【CB警告】{config['name']} {thresh['label']}発動の可能性。"
                            f"前日比{pct:+.1f}%（{direction}）"
                            f"現在値：{price:,.0f}{config['unit']}（{now} JST）"
                        )
                        subject = f"【CB警告】{config['name']} {thresh['label']}"
                        send_email(subject, body)
                        update_state(state, key)
                        print(f"[SENT] {body}")
                        notified = True

        except Exception as e:
            print(f"[ERROR] CB {symbol}: {e}")

    return notified


def check_milestones(state):
    notified = False
    for symbol, config in MILESTONES.items():
        if skip_symbol(symbol):
            continue
        try:
            price = get_price(symbol)
            if price is None:
                continue

            level = get_band(price, config["threshold"])
            print(f"[INFO] 大台チェック {config['name']}: {price:,.0f} (大台={level:,})")

            key = f"milestone_{symbol}_{level}"
            if should_notify(state, key):
                now = datetime.now(JST).strftime("%Y/%m/%d %H:%M")
                body = (
                    f"【大台突破】{config['name']}が{level:,}{config['unit']}台に到達！"
                    f"現在値：{price:,.0f}{config['unit']}（{now} JST）"
                )
                subject = f"【大台突破】{config['name']} {level:,}{config['unit']}台"
                send_email(subject, body)
                update_state(state, key)
                print(f"[SENT] {body}")
                notified = True

        except Exception as e:
            print(f"[ERROR] milestone {symbol}: {e}")

    return notified


def check_intraday_range(state):
    notified = False
    today = datetime.now(JST).strftime("%Y-%m-%d")
    tse_open = is_tse_open()

    for symbol, config in INTRADAY_RANGES.items():
        try:
            if tse_open:
                # 現物セッション: 当日の高値−安値
                high, low = get_today_hl(symbol)
                if high is None:
                    continue
                range_val = high - low
                range_desc = f"高値{high:,.0f}／安値{low:,.0f}"
                session = "現物"
                suffix = "cash"

            else:
                # 先物セッション: 東証クローズ後の初回価格を基準とする
                futures_sym = "NKD=F"
                base_key = f"futures_base_{futures_sym}_{today}"

                if base_key not in state or not isinstance(state[base_key], (int, float)):
                    base = get_price(futures_sym)
                    if base is None:
                        continue
                    state[base_key] = base
                    print(f"[INFO] 先物基準価格を記録: {base:,.0f}pt")
                    continue  # 初回は基準設定のみ

                base = float(state[base_key])
                current = get_price(futures_sym)
                if current is None:
                    continue
                range_val = abs(current - base)
                diff = current - base
                range_desc = f"基準{base:,.0f}→現在{current:,.0f}（{diff:+,.0f}pt）"
                session = "先物"
                suffix = "futures"

            print(f"[INFO] 値幅 {config['name']}（{session}）: {range_val:,.0f}{config['unit']}")

            for thresh in config["thresholds"]:
                if range_val >= thresh:
                    key = f"range_{symbol}_{thresh}_{today}_{suffix}"
                    if should_notify(state, key):
                        now = datetime.now(JST).strftime("%H:%M")
                        body = (
                            f"【値幅アラート】{config['name']}（{session}）の値幅が{thresh:,}{config['unit']}超え。"
                            f"{range_desc}（値幅{range_val:,.0f}{config['unit']}）"
                            f"（{now} JST）"
                        )
                        subject = f"【値幅アラート】{config['name']}（{session}） 値幅{thresh:,}{config['unit']}超え"
                        send_email(subject, body)
                        update_state(state, key)
                        print(f"[SENT] {body}")
                        notified = True

        except Exception as e:
            print(f"[ERROR] range {symbol}: {e}")

    return notified


def main():
    state = load_state()

    a = check_price_alerts(state)
    b = check_circuit_breakers(state)
    c = check_milestones(state)
    d = check_intraday_range(state)

    save_state(state)

    if not any([a, b, c, d]):
        print("[INFO] アラートなし")


if __name__ == "__main__":
    main()
