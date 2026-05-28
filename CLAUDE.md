# stock-monitor

GitHub Actions で15分おきに動く株価監視ボット。異常があればメールで通知する。

## 構成

```
.github/workflows/price-monitor.yml  # スケジューラ（*/15 平日のみ）
scripts/monitor.py                   # 監視ロジック本体
state/state.json                     # 通知済み記録（重複送信防止）
logs/YYYYmmdd_HHMM.log              # 実行ログ（7日分保持）
```

## 監視対象

| シンボル   | 名称         | アラート閾値 |
|-----------|-------------|------------|
| ^N225     | 日経平均     | 前日比±1000円刻み |
| NKD=F     | 日経平均先物 | 前日比±1000pt刻み（TSEクローズ中） |
| USDJPY=X  | ドル円       | 前日比±5円刻み |
| ^DJI      | ダウ平均     | 前日比±1000ドル刻み |
| ^IXIC     | ナスダック   | 前日比±500pt刻み |
| 000001.SS | 上海総合     | 前日比±100pt刻み |

その他：CB警告（±8/9/10%）、大台突破、日中値幅アラートも実装済み。

## アラートロジック

- `check_price_alerts` : 前日終値比で閾値を1段超えるごとに通知（日付単位でリセット）
- `check_circuit_breakers` : 前日比で±8%/9%/10%を超えたらCB警告
- `check_milestones` : 1万円単位の大台を突破したら通知
- `check_intraday_range` : 日中値幅が1000/2000/3000円を超えたら通知
- `notify_fetch_errors` : データ取得失敗時に1時間1回エラーメール送信

## 東証時間の扱い

- ^N225 は東証時間中のみ監視（前場 0:00-2:30 UTC / 後場 3:30-6:30 UTC）
- NKD=F は東証クローズ中のみ監視（逆）

## 問題が起きたときの確認手順

### 1. ログを確認する
```bash
git pull
ls logs/          # 最新ログを確認
cat logs/<最新ファイル>
```
`[ERROR]` や `[SKIP] xxxx: データ取得失敗` が出ていないか確認。

### 2. GitHub Actions の状態確認
リポジトリの Actions タブ → Price Monitor → 最新のrun → monitor ジョブ → Run price monitor ステップ

スケジュール実行が止まっている場合：リポジトリへのpushがあれば自動で再開する（GitHub の60日無活動ポリシー）。

### 3. 手動実行でテスト
Actions タブ → Price Monitor → "Run workflow" ボタン

### 4. メールが届かない場合
- Gmail スパムフォルダを確認
- GitHub Secrets（GMAIL_USER, GMAIL_APP_PASSWORD, NOTIFY_EMAIL）が有効か確認
- Gmailのアプリパスワードは期限切れになることがある

## よくある問題

| 症状 | 原因 | 対処 |
|------|------|------|
| アラートが来ない | Actionsが止まっている | コードをpushして再有効化 |
| データ取得失敗メールが来る | yfinanceのAPI障害 | しばらく待つ、または`get_price`の取得方法を見直す |
| 同じアラートが何度も来る | state.jsonのリセット | state/state.jsonの内容を確認 |
| メールが届かない | Gmailアプリパスワード失効 | Googleアカウントで再発行 |

## Secrets（GitHub）

- `GMAIL_USER` : 送信元Gmailアドレス
- `GMAIL_APP_PASSWORD` : Gmailアプリパスワード
- `NOTIFY_EMAIL` : 通知先メールアドレス
