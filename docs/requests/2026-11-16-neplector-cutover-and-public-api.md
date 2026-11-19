# Neplector への依頼：IdeaPad への切り替えと API の公開で、NepSync を止めっぱなしにしない

- 日付: 2026-11-16
- 依頼元: 同期サービス NepSyncSystem（発案者が Neplector の運営者に依頼）
- 状態: 対応済み（2026-11-16。Neplector の ADR 0051 と `docs/design/cutover.md`。切り替えと公開は同じ日に済み、NepSync は 16:19 から公開の住所でつながっている。**公開の住所は、下の文にある `api.<ドメイン>` ではなく `https://neplector.<ドメイン>`**。置き場所は Neplector の ADR 0052 で「1つの VM の中のコンテナ」に決まった）
- 変更するプロジェクト: Neplector（`docs/design/cutover.md` の手順。このリポジトリでは変更しない）

## 困っていること

NepSync は今、Windows の PC から `http://127.0.0.1:8787` で API を読んでいる。`cutover.md` の3（切り替え）が終わると、
Windows のこの住所には何もいなくなり、IdeaPad の API は IdeaPad の中からしかつながらない（ADR 0027）。
**API を公開する（`cutover.md` の5）まで、2つのサーバーの同期は止まる。** `cutover.md` には、API を使うサービスのことが書かれていない。

NepSync の側は準備した（2026-11-16）：止まっても5秒〜5分おきにやり直し、つながれば続きから流す。15分つながらなければ運営者に1回知らせ、戻ったらまた知らせる。
公開したあとは、NepSync の `.env` の住所を `https://api.<ドメイン>` に変えて動かし直すだけ（NepSync の `docs/design/operations.md`「Neplector が先に IdeaPad へ移るとき」）。

## 依頼

1. **公開（5）を、切り替え（3）からなるべく早く行ってほしい。** 遅くとも30日以内。変更の記録（`/v1/changes`）は30日で消えるので、それより遅れると、止まっていた間の発言の一部が流れない
2. **Cloudflare の Bot Fight Mode を有効にしないでほしい**（5の3）。人でない問い合わせを止める機能で、NepSync の問い合わせが止められる。無料の枠では、API の住所だけ例外にすることができない
3. **回数の上限（5の3）は、NepSync の使い方より上にしてほしい。** NepSync は、ふだん25秒ごとに1回（`/v1/changes?wait=25`。最大25秒つなぎっぱなしで待つ）、投票が開いているあいだは10秒ごとに1回、発言1件につき数回、5分ごとに数回、名簿のコマンドで十数回をまとめて問い合わせる。目安は「同じ IP から10秒に30回」より緩く。家の IP は1つなので、発案者がほかの機械から確かめる分も合わせて数えられる
4. **`cutover.md` の5の最後に「API を使うサービス（NepSync）の住所を変える」を1行足してほしい。** 公開したことが NepSync 側に伝わらないと、止まったままになる

## 伝えておくこと（依頼ではない）

- NepSync の bot は Neplector の bot と別（token も別）。切り替えで二重に動くことは無い
- NepSync を公開した住所からつなぐと、初めての場所からのつながりとして運営者に DM が来る（ADR 0041）。想定どおり
- `schema_version` が 2 になったこと（ADR 0048 の `conversation_pairs`）は、NepSync に影響しない。足した表は使っていない
- NepSync を Linux に移すのは公開のあと。置き場所は ADR 0038 判断3 のとおり IdeaPad の VM を考えている（VM を作るのはそのとき）。それまで Windows の PC は NepSync のために付けておく
