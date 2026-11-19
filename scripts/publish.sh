#!/usr/bin/env bash
# 公開用の枝 public を、main の今の中身から作る（開発の進め方の文書は入れない）。
# 使い方（リポジトリのフォルダで）：
#   bash scripts/publish.sh "公開する変更の説明（1行）"
#   git push public public:main
# public には main の過去のコミットは入らず、呼ぶたびに「その時点の中身」のコミットが1つ増える。
set -euo pipefail

# 公開に入れないもの（手元では使う）。足すときは、ここに書く
EXCLUDE=(CLAUDE.md config.yaml .claude tasks logs
         docs/conventions.md docs/session-handoff.md docs/direct-work.md docs/watchlist.md docs/decisions/TEMPLATE.md)
# 公開する中身に、あってはいけないもの（見つかったら止める）
FORBIDDEN='discord(app)?\.com/api/webhooks/[0-9]+/|[MN][A-Za-z0-9_-]{23,25}\.[A-Za-z0-9_-]{6}\.[A-Za-z0-9_-]{27,}|gh[opsu]_[A-Za-z0-9]{20,}|BEGIN [A-Z ]*PRIVATE KEY|\b100\.(6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\.[0-9]+\.[0-9]+\b|192\.168\.[0-9]+\.[0-9]+|neo_m|/mnt/c/Users'

message=${1:?公開する変更の説明を1行で渡してください}
cd "$(git rev-parse --show-toplevel)"
if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
    echo "コミットしていない変更があります。先にコミットしてください" >&2
    exit 1
fi

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
export GIT_INDEX_FILE="$work/index"
git read-tree HEAD
git rm -r --cached --quiet --ignore-unmatch -- "${EXCLUDE[@]}"
tree=$(git write-tree)
unset GIT_INDEX_FILE

if git grep -nIE "$FORBIDDEN" "$tree" -- . ':!scripts/publish.sh' ; then   # この台本は、上の一覧そのものに当たるので除く
    echo "公開する中身に、あってはいけないもの（上の行）があります。直してからやり直してください" >&2
    exit 1
fi

parent=$(git rev-parse -q --verify refs/heads/public || true)
if [ -n "$parent" ] && [ "$(git rev-parse "$parent^{tree}")" = "$tree" ]; then
    echo "前に公開したときから、変わったところがありません"
    exit 0
fi
commit=$(git commit-tree "$tree" ${parent:+-p "$parent"} -m "$message")
git update-ref refs/heads/public "$commit" ${parent:+"$parent"}
echo "public を $(git rev-parse --short "$commit") にしました（main の $(git rev-parse --short HEAD) から）。git push public public:main で送ります"
