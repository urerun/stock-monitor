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
            {"pct": 7,  "label": "Level 1（-7%）",  "direction": "down"},
            {"pct": 13, "label": "Level 2（-13%）", "direction": "down"},
            {"pct": 20, "label": "Level 3（-20%）終日停止", "direction": "down"},
        ],
    },
}

STATE_FILE = "state/state.json"
JST = timezone(timedelta(hours=9))


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


def get_band(price, threshold):
    return math.floor(price / threshold) * threshold


def should_notify(state, key):
    if key not in state:
        return True
    last_time = datetime.fromisoformat(state[key])
    return datetime.now(timezone.utc) - last_time > timedelta(hours=24)


def update_state(state, key):
    state[key] = datetime.now(timezone.utc).isoformat()


def build_price_message(config, price, band):
    now = datetime.now(JST).strftime("%Y/%m/%d %H:%M")
    direction = "上抜け" if price >= band + config["threshold"] / 2 else "下抜け"
    return (
        f"【{config['name']}アラート】"
        f"{band:,.0f}{config['unit']}を{direction}。"
        f"現在値：{price:,.2f}{config['unit']}（{now} JST）"
    )


def build_cb_message(config, label, pct, price):
    now = datetime.now(JST).strftime("%Y/%m/%d %H:%M")
    direction = "上昇" if pct > 0 else "下落"
    return (
        f"【CB警告】{config['name']} {label}発動の可能性。"
        f"前日比{pct:+.1f}%（{direction}）"
        f"現在値：{price:,.0f}{config['unit']}（{now} JST）"
    )


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
        try:
            price = get_price(symbol)
            if price is None:
                print(f"[SKIP] {symbol}: データ取得失敗")
                continue

            band = get_band(price, config["threshold"])
            print(f"[INFO] {config['name']}: {price:,.2f} (band={band:,.0f})")

            key = f"{symbol}_{band}"
            if should_notify(state, key):
                body = build_price_message(config, price, band)
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
                        body = build_cb_message(config, thresh["label"], pct, price)
                        subject = f"【CB警告】{config['name']} {thresh['label']}"
                        send_email(subject, body)
                        update_state(state, key)
                        print(f"[SENT] {body}")
                        notified = True

        except Exception as e:
            print(f"[ERROR] CB {symbol}: {e}")

    return notified


def main():
    state = load_state()

    a = check_price_alerts(state)
    b = check_circuit_breakers(state)

    save_state(state)

    if not a and not b:
        print("[INFO] アラートなし")


if __name__ == "__main__":
    main()
