# NepSync

2つの Discord サーバーの決まったカテゴリを、片方向または双方向に同期する bot です。
発言・スレッド・リアクション・投票・ピン留め・チャンネルの形を、相手のサーバーに写します。

発言の中身は、Discord から直接は受け取りません。記録のサービス **Neplector** の API から読みます。
Neplector は別のプロジェクトで、このリポジトリには入っていません。

```
 サーバーA ──記録──┐                         ┌── Webhook で投稿 ──> サーバーA / B
                   ├─> Neplector ──API──> NepSync
 サーバーB ──記録──┘   （読むだけ）          └── bot で構造を操作 ─> サーバーA / B
                                              ↕
                                        対応表（SQLite）
```

## できること

- **発言の同期**：写しは Webhook で、書いた人の名前とアイコンで投稿します。編集・削除・返信の引用・添付（20 MiB まで）も写します
- **メンション**：相手のサーバーにだけいる人へのメンションは、相手のサーバーで本当に通知します。写しに返信すると、元の発言の人に通知します
- **構造の同期**：両方のサーバーの同じ名前のカテゴリを見比べ、チャンネル・スレッドの差をそろえます。チャンネルの削除には、1時間あたりの上限と確認があります
- **リアクション・投票・ピン留め**：リアクションは bot が同じ絵文字を付けます。投票は、両方の票の合計を別の発言に書きます
- **告知**：告知チャンネルの発言を、右クリックのメニューで選んだものだけ、相手のサーバーに出します
- **コマンド**
  - `/nepsync-members`：相手のサーバーの人を、アイコンの格子の画像で、打った人にだけ見せます。名前で探せます。選んだ人の `<@ID>` を出すので、それをコピーして呼べます
  - `/nepsync-hide`・`/nepsync-show`：自分を名簿に載せない・載せるに戻します
  - 発言のメニュー：「リアクションした人」「投票した人」「この人を呼ぶ」

## 守っていること

- **発言の本文を保存しません**。手元に残すのは、ID の対応、構造の控え、読んだ位置だけです。ログにも本文を書きません
- **Discord から発言の知らせを受けません**。常時接続で受けるのは、サーバー・チャンネル・スレッド・ロールの形（GUILDS）だけです。Message Content も使いません
- Neplector から外れた人の写しと設定は消します

## 動かす

Python 3.14 で作っています。

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt     # Windows は .venv\Scripts\pip
cp .env.example .env                          # Neplector の API の住所とキー、bot の token、運営者への知らせの Webhook
mkdir -p data && cp nepsync.example.toml data/nepsync.toml   # 同期するサーバーとカテゴリ
cd work
python -m nepsync doctor    # 設定と権限を確かめる（何も変えない）
python -m nepsync plan      # 構造でこれからすることを見る（何も変えない）
python -m nepsync run       # 動かす
```

- 秘密の値は、環境変数でも渡せます（環境変数が先、無ければ `.env`）
- 同じ `data/` で2つ動かさないでください（発言が二重に流れます）
- コンテナで動かす形は `deploy/`（`Dockerfile`・`compose.yaml`）にあります
- 名簿の画像に日本語を出すには、日本語の字体が要ります（コンテナでは `fonts-noto-cjk`）

## テスト

```bash
cd work
python -m unittest discover -s tests -t .
```

テストは架空のサーバーと架空の会話だけを使い、Discord と Neplector にはつなぎません。

## 文書

- `docs/proposal.md`・`docs/requirements.md`：何を作るか
- `docs/design/overview.md`：全体の形。`docs/design/operations.md`：動かし方・移し方
- `docs/decisions/`：判断の記録（ADR）。なぜこの形にしたかは、ここにあります

## ライセンス

ライセンスは付けていません。コードを読むことはできますが、使う・改変する・配る許可は出していません。
