import yfinance as yf
import json
import os
import smtplib
import math
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timedelta, timezone

SYMBOLS = {
    "^N225":    {"name": "日経平均",  "threshold": 1000, "unit": "円"},
    "USDJPY=X": {"name": "ドル円",    "threshold": 5,    "unit": "円"},
    "^GSPC":    {"name": "S&P500",   "threshold": 500,  "unit": "ドル"},
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


def get_band(price, threshold):
    """現在価格がどの閾値帯にいるか（下限値）を返す"""
    return math.floor(price / threshold) * threshold


def should_notify(state, symbol, band):
    """同一閾値帯への通知を24時間以内に重複しない"""
    key = f"{symbol}_{band}"
    if key not in state:
        return True
    last_time = datetime.fromisoformat(state[key])
    return datetime.now(timezone.utc) - last_time > timedelta(hours=24)


def update_state(state, symbol, band):
    key = f"{symbol}_{band}"
    state[key] = datetime.now(timezone.utc).isoformat()


def build_message(config, price, band):
    """140字以内の通知文を生成"""
    now = datetime.now(JST).strftime("%Y/%m/%d %H:%M")
    direction = "上抜け" if price >= band + config["threshold"] / 2 else "下抜け"
    return (
        f"【{config['name']}アラート】"
        f"{band:,.0f}{config['unit']}を{direction}。"
        f"現在値：{price:,.2f}{config['unit']}（{now} JST）"
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


def main():
    state = load_state()
    notified = False

    for symbol, config in SYMBOLS.items():
        try:
            price = get_price(symbol)
            if price is None:
                print(f"[SKIP] {symbol}: データ取得失敗")
                continue

            band = get_band(price, config["threshold"])
            print(f"[INFO] {config['name']}: {price:,.2f} (band={band:,.0f})")

            if should_notify(state, symbol, band):
                body = build_message(config, price, band)
                subject = f"【価格アラート】{config['name']} {band:,.0f}{config['unit']}帯"
                send_email(subject, body)
                update_state(state, symbol, band)
                print(f"[SENT] {body}")
                notified = True

        except Exception as e:
            print(f"[ERROR] {symbol}: {e}")

    save_state(state)

    if not notified:
        print("[INFO] アラートなし")


if __name__ == "__main__":
    main()
