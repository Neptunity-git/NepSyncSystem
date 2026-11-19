# 運用：動かす・止める・IdeaPad の VM に移す

判断の理由は [ADR 0003](../decisions/0003-two-sites-and-always-on.md)・[ADR 0013](../decisions/0013-move-to-container-in-vm.md)。

今（2026-11-16 から）：Neplector は IdeaPad（`neo-pve`）の VM-1 で動き、NepSync は Windows の PC から、公開された API `https://neplector.<ドメイン>` を読んで動いている。移す先は VM-2（`neptunity-services`）のコンテナ。

## 守ること

- **同時に2つ動かさない。** 発言が二重に流れる。同じ機械では鍵（`data/nepsync.lock`）で防げるが、**別の機械どうしでは防げない**。移すときは、必ず先に止める
- `.env` と `data/` はコミットしない。`data/nepsync.db` には Webhook の token が入っているので、人に渡さない

## ふだんの確認（`work` で実行）

| コマンド | すること |
|---|---|
| `python -m nepsync doctor` | 設定と権限を確かめる。何も変えない |
| `python -m nepsync plan` | 構造で次にすることを見る。何も変えない |
| `python -m nepsync approve-deletes` | 上限で止めた削除を、確かめたあとで進める |

運営者への知らせは、`OPS_WEBHOOK_URL` のチャンネルに届く。

## 告知チャンネル（ADR 0009）

組にした告知チャンネルの告知は、自動では流れない。発言を右クリック →「アプリ」→「相手のサーバーにも出す」で選んだものだけが、相手の告知チャンネルに出る。下げるときは「相手のサーバーから下げる」（どちらのサーバーの側で押してもよい）。答えは押した人にだけ見える。

始めるとき（1回だけ）：

1. 相手のサーバーで、**告知チャンネルのフォローを外す**（チャンネルの設定 →「連携サービス」→ フォロー中のチャンネル）。外さないと、同じ告知が2つ出る
2. 両方の告知チャンネルで、bot に View Channel・Manage Webhooks・Read Message History・Add Reactions を許可する
3. `data/nepsync.toml` に組と通知するロールを書く：
   ```toml
   announcements = [["KGU の告知チャンネルの ID", "CID の告知チャンネルの ID"]]
   announce_ping_roles = ["member"]
   ```
4. `python -m nepsync doctor` で確かめてから、動かし直す（メニューは起動したときに登録される）

Neplector が読んでいない告知チャンネルからは出せない（「記録が無い」と答える）。今の組は、両方とも Neplector が読んでいる（2026-11-10）。`doctor` で確かめられる。

## Windows（今）

- タスク「NepSync\run」が、ログインしたときと5分ごとに `scripts\run.cmd` を起こす。止まっても5分以内に戻る
- ログ：`data\logs\run.log`
- **ログインしているあいだだけ動く**。PC を落とすと両拠点の同期が止まる（Neplector は IdeaPad で動き続ける）。再開すれば続きから流れ、遅れた発言には印が付く（変更の記録が残っている30日以内）。**VM に移すまで、PC は付けておく**
- 止める：`Disable-ScheduledTask -TaskPath '\NepSync\' -TaskName run` と `Stop-ScheduledTask -TaskPath '\NepSync\' -TaskName run`。5分ごとの見回りで戻らないように、**必ず Disable も行う**
- 動かす：`Enable-ScheduledTask -TaskPath '\NepSync\' -TaskName run` と `Start-ScheduledTask -TaskPath '\NepSync\' -TaskName run`
- コードを変えたら：止めて動かし直す
- **`Stop-ScheduledTask` では Python が残る**（`run.cmd` だけが止まる。2026-11-10 に確かめた）。止めたあと、`nepsync run` の python.exe が残っていないかを見て、残っていれば止める：`Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*nepsync run*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }`

## IdeaPad の VM-2（コンテナ）に移す

移す先は、IdeaPad（`neo-pve`）の VM-2 `neptunity-services` の中の、NepSync 用のコンテナ（ADR 0013）。VM-2 の形・入り方・ひな形・戻し方の正は Neplector の [services-vm.md](../../../Neplector/docs/design/services-vm.md)（1〜5）。ここには NepSync の分だけを書く。

決まっていること：

- 担当が入るのは VM-2 のユーザー `nepsync` だけ（`sudo` は無い）。VM-1（Neplector）と土台には入らない・置かない
- 鍵は NepSync 専用の `~/.ssh/neo_services_nepsync`（2026-11-18 に作った）。ほかの鍵を使い回さない
- 入る：`ssh -i ~/.ssh/neo_services_nepsync nepsync@<VM-2 の Tailscale の住所>`（住所は Neplector の services-vm.md の「5」）。長いコマンドは Bash ツールから `ssh … 'bash -s' <<'EOF' … EOF` で送る（PowerShell からだと `"` が抜ける）
- 外に公開しない（入ってくるつながりは要らない）。Cloudflare Tunnel も入れない
- API は、今と同じ `https://neplector.<ドメイン>` で読む。キーも今のものを使う
- 設定ファイルはリポジトリの [deploy/compose.yaml](../../deploy/compose.yaml)・[deploy/Dockerfile](../../deploy/Dockerfile)。上限の最初の値はメモリ 256MB・CPU 0.5
- VM-2 のフォルダ：

  ```
  ~/service/
    compose.yaml   deploy/compose.yaml を写したもの
    .env           Windows の .env の値（権限 600）
    data/          nepsync.toml・nepsync.db（・nepsync.lock）。コンテナの /app/data
    src/           requirements.txt・work/・deploy/（git archive で送る）
  ```
- 写し：`~/service/backup.sh` は置かない。VM-2 の丸ごとの写し（毎日 3:45、7日分）で足りる（ADR 0013 の追記）

流れ（順番は変えない）：

1. **先に済ませる**（済み 2026-11-18）：秘密の値を環境変数で受け取る、`compose.yaml`・`Dockerfile` を書く、鍵を作る
2. **ユーザーを作ってもらう**：発案者を通して、Neplector のプロジェクトに `add-service nepsync 410M`（公開鍵は `~/.ssh/neo_services_nepsync.pub`。公開しない）を頼む。410M は「コンテナの 256m ＋ Docker の係 150MB」
3. **VM-2 の中で用意する**（Windows はまだ動かしたまま。リポジトリのフォルダの Bash で）

   ```bash
   K=~/.ssh/neo_services_nepsync; H=nepsync@<VM-2 の Tailscale の住所>
   ssh -i $K $H 'mkdir -p ~/service/src ~/service/data && chmod 700 ~/service'
   git archive HEAD requirements.txt work deploy | ssh -i $K $H 'tar x -C ~/service/src'
   scp -i $K deploy/compose.yaml $H:service/compose.yaml
   scp -i $K .env $H:service/.env && ssh -i $K $H 'chmod 600 ~/service/.env'
   ssh -i $K $H 'cd ~/service && docker compose build'
   ```

   **まだ `up` しない**
4. **Windows の同期を止める**（ここから先は同期が止まる）

   ```powershell
   Disable-ScheduledTask -TaskPath '\NepSync\' -TaskName run
   Stop-ScheduledTask -TaskPath '\NepSync\' -TaskName run
   ```

   `python.exe` が残っていないことを確かめる（上の「Windows」）。**止める前に VM-2 で動かさない**（別の機械どうしでは鍵で防げず、発言が二重に流れる）
5. **データを写す**：`data/nepsync.toml` と `data/nepsync.db` を `~/service/data/` へ（`nepsync.lock` と `logs/` は写さない）。読む位置と対応表は `nepsync.db` に入っているので、これを写さないと、写しの編集・削除・外れた人の処理ができなくなる。Windows のものは消さずに残す（戻せるように）

   ```bash
   scp -i $K data/nepsync.toml data/nepsync.db $H:service/data/ && ssh -i $K $H 'chmod 600 ~/service/data/*'
   ```
6. **確かめる**：`ssh -i $K $H 'cd ~/service && docker compose run --rm app python -m nepsync doctor'`。VM-2 は初めての場所なので、Neplector から運営者に DM が来る（想定どおり。Neplector ADR 0041）
7. **動かす**：`docker compose up -d` → `docker compose ps` → `docker compose logs --tail 50 app` で「動かします。読む位置: …」を見る → `docker stats --no-stream` で量を見て、`mem_limit` を直す
8. **知らせる**：発案者に、動いたこと・使っている量を伝える（Neplector のプロジェクトが services-vm.md の「5」の表を直す）
9. **1週間ほど発言が流れたら、Windows の片付け**：`Unregister-ScheduledTask -TaskPath '\NepSync\' -TaskName run`、`scripts\` を消す、Windows の `data\nepsync.db` と `.env` を消す（token が残るため）

止まっていた間の発言は、続きから流れて遅れた印が付く。30日を超えて止めると読めない部分ができるので、その前に終える。

**戻すとき**（VM-2 でうまく動かない）：VM-2 で `docker compose down` → 止まったことを確かめる → Windows で `Enable-ScheduledTask`・`Start-ScheduledTask`。VM-2 で動いた分の `nepsync.db` は Windows に無いので、VM-2 で長く動いたあとなら、先に `nepsync.db` を Windows へ写し戻す。

### VM-2 でのふだんの操作（`~/service` で）

| したいこと | コマンド |
|---|---|
| 状態・ログ | `docker compose ps`・`docker compose logs --tail 100 app` |
| コードを変えた | 手順3の `git archive` で送り直す → `docker compose up -d --build` |
| 止める・動かす | `docker compose down`・`docker compose up -d` |
| 確かめる・削除を進める | `docker compose exec app python -m nepsync doctor`（`approve-deletes` も同じ形） |
| イメージを新しく（月1回ほど） | `docker compose build --pull && docker compose up -d` → `docker image prune -f` |

## Neplector が IdeaPad へ移ったときの記録（2026-11-16。済み）

Neplector の切り替えで、Windows の `127.0.0.1:8787` には何もいなくなった。NepSync は止めずに待ち、API が公開されてから住所を変えた。

- 15:42 から同期が止まり、15:57 に「15分つながらず、同期が止まっています」が運営者のチャンネルに届いた（想定どおり）
- `.env` の `NEPLECTOR_BASE_URL` を `https://neplector.<ドメイン>` に変えて動かし直し、16:19 から再開した
- 動かし直したので「つながりが戻りました」は届かない（止まったことは動いているプロセスだけが覚えている）
- 住所は、前に書いていた `api.<ドメイン>` ではなく `neplector.<ドメイン>`（Neplector の ADR 0051）

**API の住所がまた変わるとき**は、同じ手順：`.env` の `NEPLECTOR_BASE_URL` を変える（末尾の `/` は要らない。`.env` の中身は表示しない）→ `python -m nepsync doctor` → 動かし直す（上の「Windows」の、止める → `python.exe` が残っていないか見る → 動かす）。

## API を公開の住所で読むあいだ、守ること

- **Cloudflare で、NepSync を弾かない**（Neplector 側の設定。Neplector の `docs/design/cutover.md`）
  - Bot Fight Mode（人でない問い合わせを止める機能）を有効にしない。NepSync の問い合わせは人でないので止められ、無料の枠では例外を作れない
  - 回数の上限は、NepSync の使い方より上にする。NepSync は、ふだん25秒ごとに1回（新しい変更を最大25秒待つ問い合わせ）、投票が開いているあいだは10秒ごとに1回、発言1件につき数回、5分ごとの構造の見回りで数回、`/nepsync-members` の名簿で数回をまとめて問い合わせる（名前の入力候補は、打つたびではなく1分に数回。アイコンは Discord から取るので数えない）。目安は「10秒に30回」より緩く
  - 家の IP アドレスは1つなので、同じ家から使うほかのもの（発案者の確かめなど）と合わせて数えられる
- Cloudflare や家の回線が止まると、同期も止まる。戻れば続きから流れる（30日以内）
