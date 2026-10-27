#!/usr/bin/env bash
# Before pushing to a public repository: looks for what must not be published
# in the files of a branch and in everything its history ever added.
#   suite/check-public.sh                                   this repository, main
#   suite/check-public.sh claude-swap chenyu-account-policy another repository and branch
# Keys and tokens fail the check; local paths fail in the files and are listed
# for the history; e-mail addresses are only listed, to look over.
set -euo pipefail

repo="${1:-$(cd "$(dirname "$0")/.." && pwd)}"
ref="${2:-main}"
cd "$repo"
git rev-parse --verify --quiet "$ref^{commit}" >/dev/null || { echo "没有 $ref 这个分支或提交" >&2; exit 2; }

secret='sk-ant-[a-z0-9]+-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|-----BEGIN [A-Z ]*PRIVATE KEY-----|AKIA[0-9A-Z]{16}'
paths="/home/${USER:-nobody}/|/Users/${USER:-nobody}/|/mnt/[a-z]/|$(hostname | sed 's/[.]/[.]/g')"
email='[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}'
fine_email='@(example\.(com|org|net|invalid)|users\.noreply\.github\.com|token\.local)$'
failed=0

section() { printf '\n== %s\n' "$1"; }
# a search that finds nothing is not an error here (pipefail): only the output counts
quiet() { "$@" || true; }
# prints its input indented, at most 20 lines; returns 1 when there was any
show() { local out; out="$(head -20)"; [ -z "$out" ] && return 0; printf '%s\n' "$out" | sed 's/^/   /'; return 1; }

section "密钥、令牌（$ref 的文件）"
quiet git grep -nIE "$secret" "$ref" -- . | show || failed=1
section "本机路径和主机名（$ref 的文件）"
quiet git grep -nIE "$paths" "$ref" -- . | show || failed=1
section "密钥、令牌（$ref 的全部历史）"
git log -p --no-color "$ref" | quiet grep -E '^\+' | quiet grep -E "$secret" | show || failed=1
section "本机路径（$ref 的历史，只列出，不算失败）"
git log -p --no-color "$ref" | quiet grep -E '^\+' | quiet grep -oE "$paths" | sort | uniq -c | show || true
section "邮箱地址（$ref 的文件，只列出：看看有没有不该公开的）"
quiet git grep -hoIE "$email" "$ref" -- . | quiet grep -vE "$fine_email" | sort | uniq -c | sort -rn | show || true
section "不要推送的引用"
git for-each-ref --format='%(refname)' refs/claude-review | show || echo "   （上面这些只在本机：推送时只写分支名，不要用 --all、--mirror）"

if [ "$failed" = 1 ]; then
  printf '\n有不能公开的内容，先处理再推送。\n'
  exit 1
fi
printf '\n没有发现密钥、令牌或本机路径。\n'
