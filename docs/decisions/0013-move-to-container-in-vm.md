# 0013: 移す先は、IdeaPad の VM の中のコンテナにする（systemd で機械に直接置く形をやめる）

- 日付: 2026-11-16
- 状態: 採用中（2026-11-16 に発案者が採用。方針を変えたら、このファイルは書き換えず、新しい ADR を作ってここを「廃止（NNNN で置き換え）」にする）
- 関連: [0003](0003-two-sites-and-always-on.md) 判断3（「移す先は Linux の systemd」。この ADR で、移す先の部分を置き換える）、Neplector の ADR 0052（サービスは1つの VM の中のコンテナ）・0051（API の住所）、teamNeo の ADR-0016 判断5

## きっかけ

2026-11-16 に、Neplector の本番が IdeaPad（`nep-host`）へ移り、API が `https://neplector.<ドメイン>` で公開された。NepSync は同じ日の 16:19 から、Windows の PC のまま、この住所で動いている。
0003 判断3 の「後で見直す条件」（API を公開して Linux へ移すとき）に当たる。同じ日に、Neplector の側で、発案者のサービスの置き方が決まった（ADR 0052）。

## 判断1: 置き場所は、IdeaPad の1つの VM の中の、NepSync 用のコンテナ（発案者。Neplector の ADR 0052）

**決めたこと**：

- NepSync は、IdeaPad の VM の中で、コンテナ1つとして動かす。`nep-host` に直接置かない
- API は、今と同じ公開の住所 `https://neplector.<ドメイン>` で読む。Neplector のファイルには触れない
- NepSync の担当（Claude）が入るのは VM だけ。VM の中の NepSync 用のフォルダに、コンテナの設定ファイルと秘密の値を置く
- 0003 判断3 で用意した `deploy/nepsync.service`（systemd の設定）と、専用の OS ユーザーを作って `/opt/nepsync` に置く手順は、使わない。`deploy/nepsync.service` は消した（2026-11-16。前の形は git の履歴にある）

**やめた形と理由**：systemd で機械に直接置く・OS のユーザーで分けるだけ、は Neplector の ADR 0052 判断1 の案C・案D で却下されている（OS を Neplector と共有するので、bot の token や本文に届きやすい）。

## 判断2: コンテナで動かせる形にするために、NepSync の側で変えること（Claude の案）

| 決まり（teamNeo の conventions） | 今 | すること |
|---|---|---|
| 設定は環境変数で受け取る | 秘密の値は `.env` のファイルからだけ読む（`work/nepsync/config.py` の `load_secrets`） | 環境変数に値があればそれを使い、無ければ今までどおり `.env` を読む。Windows の動かし方は変わらない（2026-11-16 に済み） |
| つながらなければ、待ってやり直す | できている（5秒〜5分おきにやり直し、15分続くと運営者に知らせる） | 何もしない |
| 使う量の目安を書く | 書いていない | コンテナの設定ファイルに上限を書く。最初の値（案）：メモリ 256MB・CPU 0.5・ログ 10MB×3。VM で動かして実際の量を見てから直す |
| 動いている状態の置き場所 | `data/nepsync.toml`、`data/nepsync.db`（と `.env`） | `data/` を、コンテナの外のフォルダとしてつなぐ（コンテナを作り直しても残るように） |

「同じ data のフォルダで2つ動かさない」（要件 N9）は変わらない。別の機械どうしでは鍵で防げないので、移すときは Windows を先に止める。

## 判断3: 移すのは、VM ができて、Neplector からひな形が届いてから（Claude の案）

**決めたこと**：それまで、Windows の PC で動かし続ける。コンテナの設定ファイル（Dockerfile など）は、Neplector から VM への入り方・フォルダの形・設定ファイルのひな形が届いてから書く。環境変数の対応（判断2 の1行目）は、先に済ませてよい。

**選んだ理由**：ひな形の前に設定ファイルを書くと、合わせ直しになる。VM は 2026-11-17 の夜以降に作られる（Neplector の ADR 0052「始める時期」）。

## 引き換え

- 同じ機械の中なのに、API を読むたびに一度インターネットへ出て戻る。Cloudflare や家の回線が止まると、同期も止まる（Neplector の ADR 0052 判断3 で受け入れ済み。戻れば続きから流れる）
- IdeaPad が止まると、Neplector と同期がいっしょに止まる
- 移したあとは、Windows の PC を付けておかなくてよくなる

## 追記（2026-11-18）：置き場所は VM-2。写しは VM の丸ごとの写しに任せる

- **置き場所**：Neplector の側で形が変わり（Neplector の ADR 0057・0058、teamNeo の ADR-0019）、IdeaPad は Proxmox VE の `neo-pve` の上に VM が3つになった。`nep-host` も「1つの VM」も無い。NepSync は Neptunity のサービスなので **VM-2 `neptunity-services`**。判断1 の中身（VM の中のコンテナ・Neplector に触れない・担当は VM だけ）は変わらない
- **入り方**：VM-2 のユーザー `nepsync`（rootless の Docker、`sudo` 無し）に、専用の鍵 `~/.ssh/neo_services_nepsync` で入る。外には公開しない
- **写し**（未決定だったもの。Claude の案）：`~/service/backup.sh` は置かない。VM-2 の丸ごとの写し（毎日 3:45、HDD、7日分）で足り、NepSync だけを戻す方法（services-vm.md の「7」A）もある。`nepsync.db` は ID の対応と読む位置だけで、作り直しのきかない記録ではない。1日分を失って戻しても、続きから読み直せる
- 手順は `docs/design/operations.md`「IdeaPad の VM-2（コンテナ）に移す」

## 未決定（VM-2 で動かしてから決める）

- 上限の数字（判断2 の案。`docker stats` を見てから直す）
- 止まったときの見張り（今は、運営者のチャンネルへの知らせだけ）

## 用語メモ

- VM（仮想マシン）：1台の機械の中に、OS ごと別の機械を作る仕組み
- コンテナ（Docker）：プログラムを、動くのに必要なものごと箱に詰めて動かす仕組み
- 環境変数：プログラムを起動するときに、外から渡す設定の値
- systemd：Linux で、プログラムを動かし続ける標準の仕組み
