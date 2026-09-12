---
name: raw-nul-in-source-makes-git-treat-it-as-binary
description: 落とし穴(2026-08-15実測) — ソースに生のNULバイトを1つ書くと、gitがそのファイルをバイナリ扱いし、diff・blame・改行の正規化が黙って止まる。動作は正常なので気づけない
created: 2026-08-15
updated: 2026-08-15
---

duocadの `web/src/viewport.ts` に、面の強調でキーを突き合わせる区切りとして
**生のNULバイト**が2個書かれていた（エスケープ表記ではなく、文字そのもの）:

```
const keys = new Set(refs.map((ref) => `${ref.bodyId}<生のNUL>${ref.faceIndex}`))
```

**実行時は正しく動く**（JSの文字列はNULを含められる）。壊れるのは道具の側:

- **gitがNULを見てバイナリと判定する**（先頭8000バイトの走査。`git ls-files --eol` が `i/-text`）
- その結果 **`git diff` も `git blame` も効かない**。`git log -p` は「Binary files differ」しか出さない
- **`core.autocrlf` の正規化も止まる**。他のファイルはgitがLFへ直すのに、このファイルだけ
  書いたツールの改行がそのまま格納される（[[pathlib-write-text-crlf-on-windows]] の事故はこれで表面化した）

しかも**このファイルはリポジトリで最もchurnの激しいファイル**（three.jsの囲い）だった。
差分が読めない状態が5日以上続いていたが、動作には出ないので誰も気づいていない。

**直し方**: Unicodeのエスケープ表記へ置き換える（実行時の値は同じ）。
これでテキストに戻り、diff・blame・改行の正規化がすべて効くようになる。

**見つけ方**: `git ls-files --eol` に `i/-text` が出ているテキストファイルを疑う。
`git diff --stat` と `git diff --ignore-all-space --stat` が桁違いなのも同じ兆候。
