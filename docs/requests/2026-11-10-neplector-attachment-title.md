# Neplector への仕様変更の依頼：添付の元のファイル名（`title`）を API で渡す

- 日付: 2026-11-10
- 依頼元: 同期サービス NepSyncSystem（発案者が Neplector の運営者に依頼）
- 状態: 一部対応（2026-11-10 02:05、Neplector ADR 0045。下の「対応のあとに残る心配」）
- 変更するプロジェクト: Neplector（このリポジトリでは変更しない）

## 依頼

`/v1/messages` の `attachments` の各項目に、Discord の添付の `title` を足してください。

```json
{"attachment_id": "…", "extension": "pdf", "url": "…", "title": "第3回 議事録（確定）"}
```

Discord が `title` を返さない添付（英数字だけの名前など）では、`null` で構いません。

## 困っていること

添付を送り直すサービスは、ファイル名を添付の `url` から取るしかありません。Discord はこの名前を書き換えるので、日本語が失われます。

実際に Webhook で送って確かめました（2026-11-10）。

| 送った名前 | `filename` と URL の名前 | `title` |
|---|---|---|
| `議事録.png` | `f17df61cc7237989.png`（元の名前と関係ない文字列） | `議事録` |
| `第3回 議事録（確定）.pdf` | `3_.pdf` | `第3回 議事録（確定）` |
| `議事録 2026.png` | `2026.png` | `議事録 2026` |
| `SPOILER_議事録 2026.png` | `SPOILER__2026.png` | `SPOILER_議事録 2026` |
| `report v2 final.png` | `report_v2_final.png` | `report v2 final` |
| `SPOILER_architecture.png` | `SPOILER_architecture.png` | なし |

- 元の名前が残るのは `title` だけ
- 日本語の資料を共有するコミュニティでは、受け取った人がファイルを見分けられない
- サービスの bot は Message Content を有効にしていないので、Discord からも添付の情報を取れない（Neplector の決まりどおり）。Neplector の API のほかに手段が無い

## 判断してほしいこと

名前の記録は、Neplector の今の決まりを変えることになります。**運営者として判断してください。**

1. **ADR 0036 判断7「ファイル名は残さない」を変えるか。** 理由は「人の情報を含むことがあるので」。`title` を記録すると、この判断を置き換えることになる
2. **告知をどうするか。** 両方のサーバーの告知は、添付の記録を「ファイルは保存せず、URL・大きさ・種類を記録」と書いている。ファイル名は書いていない。ADR 0042 は「告知は1回だけ」で、書いていないことを始めるなら改めて知らせる必要がある
3. **どれだけ残すか。** 下の案を参照

### 参考：今も名前の一部は記録されている

`attachment_files.url` には、Discord が書き換えた名前が入っている（記録済みの 1,972 件すべてで、URL の最後が記録の拡張子で終わることを確かめた）。この表は API の表の窓口で、`member` のキーを持つ人が読める。「ファイル名は残さない（拡張子だけ）」は、英数字の名前については、今も厳密には成り立っていない。この点の扱いも、あわせて見直してください。

## 提案

判断の材料です。決めるのは運営者です。

| 項目 | 提案 | 理由 |
|---|---|---|
| 扱い | 本文と同じ（ADR 0040 の `forward_texts` と同じ形） | 人が付けた文で、本文に近い内容が入ることがある |
| 置き場所 | 新しい表（例：`attachment_titles`）。`attachment_files` の列にしない | `api.py` の `HIDDEN_TABLES` で表の窓口から隠すため |
| 渡し方 | `/v1/messages` の `attachments[].title` だけ。`api.bodies_from` の日から | 本文と同じ条件 |
| 記録する発言 | 人の発言だけ | 本文の記録と同じ条件（bot の投稿は残さない） |
| AI | 渡さない | 発言の内容ではなく、読み取りに要らない |
| 残す期間 | **30日を提案** | 同期に使うのは受け取った直後。`changes` が30日で消えるので、止まっていた分を流し直せる長さで足りる。本文の1年に合わせると、要る以上に長く持つことになる |
| 消すとき | 消された発言・外れた人の発言・消されたチャンネルの分を消す | 本文と同じ |
| 本文の上限の数え方 | 今の `/v1/messages` の数え方のまま | 新しい窓口を増やさないため |

他のサービス（検索・ナレッジなど）で長く使いたくなったら、1年に延ばす判断もあり得ます。

### 直す場所の見当

- `models.py`：`AttachmentFile` に `title`（`_attachment_file` で `item.get("title")`）
- `db.py`：表を足す（版は `schema-changes.md` のとおり）
- `ledger.py`：`forward_texts` を書く処理と同じ「人の発言」の条件で書く
- `api.py`：`HIDDEN_TABLES` に足す。`_messages` で、開いているときだけ `attachments` に足す
- `purge.py`・`private_state.py`：消す処理と期限
- `developer_guide.md` の 7：`title` を足す。「ファイル名は記録していません」を直す
- `docs/design/data-model.md`・`schema-changes.md`・ADR

### 確かめてほしいこと

テストは架空のデータで。

- `title` のある添付で、`/v1/messages` に `title` が出る
- 開く前（`bodies_from` より前）は `null`
- 表の窓口に出ない（`/v1/meta` の一覧に無く、`/v1/tables/<表>` が 404）
- bot の投稿の `title` は残らない
- 外れた人の発言、消された発言の分が消える
- 期限を過ぎた分が消える
- `title` が無い添付でも今までどおり動く
- **本物の知らせ**で `title` が届くこと。Webhook の答えには入っていたが、ゲートウェイの `MESSAGE_CREATE` と、取り込みの REST でも入るかは、こちらでは確かめられない（サービスの bot は発言を読めない）

## サービス側の対応

NepSync は対応済みです（`work/nepsync/rewrite.py` の `attachment_filename`）。

- `title` があれば、`title` と記録の拡張子から名前を作る
- 無ければ、今までどおり URL から取る
- `/v1` は項目を足しても壊れない約束なので、Neplector の変更は、どの順番で入れても動く
- 名前は保存しない

変更が入ったら教えてください。本物のファイルで確かめます。

## 急ぎ具合

急ぎません。今は英数字の名前は引き継げていて、日本語の部分だけが落ちます。添付は同期できています。

## 対応のあとに残る心配（2026-11-10 追記）

Neplector は `attachments[].filename` を足した（Neplector ADR 0045）。ありがとうございます。NepSync はこれを使うようにした。

ただし、記録しているのは Discord の添付の **`filename`** で、依頼した **`title`** ではない（`models.py` の `_attachment_file` が `item["filename"]`）。上の表のとおり、Webhook で送ったときは、日本語は `filename` から落ちて `title` にだけ残った。人が Discord のアプリから貼ったときも同じなら、日本語の名前は届かない。

確かめてほしいこと：

1. 日本語の名前のファイルを人が貼ったときの、ゲートウェイの `MESSAGE_CREATE` の添付に `title` があるか、`filename` が日本語のままか
2. `title` があるなら、`title` も記録してほしい（`filename` と同じ扱いで）。NepSync は `title` を優先し、拡張子は `extension` から付ける。項目の名前は `title` のままがありがたい（NepSync はそれを読む作りになっている）

02:14 より後の添付は、2026-11-10 02:27 の時点で 0 件だったので、こちらではまだ確かめられていない。
