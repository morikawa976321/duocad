---
name: duocad-save-location-fixed-folder
description: 判断(2026-08-22) — duocadの保存先をリポジトリ直下 duocad/models/ に固定する。パス手打ちをやめ、アプリ内UIの一覧から開く形にする。当初のgitignoreは2026-08-27に取りやめ
created: 2026-08-22
updated: 2026-08-28
---

# duocad: 保存先は `duocad/models/`、開くのはアプリ内の一覧から

(gitignoreは2026-08-27に外した → [[duocad-model-files-are-disposable]]。以下の「ignoreする」は2026-08-22当時の選択)

## 判断（本人）

> 毎回パスを手打ちするのではなく、保存場所を特定のフォルダにきめておいて、
> アプリ内UIからGUIで開けるようにしたい。
> 2はduocad/にモデルフォルダを追加しignoreする。

保存先候補として `%USERPROFILE%\Documents\duocad\` と `3d-models` と並べる案、
リポジトリ内の3つを出し、**リポジトリ内（`duocad/models/`）＋gitignore** を選んだ。
AI側は「gitに載る」を理由にリポジトリ内を避ける案を出していたが、
**gitignoreすればその懸念は消える**ので却下された。

## 何が問題だったか

保存/開く自体は既に実装済みでUIボタンまで通っていたが、
引数が**テキスト入力のパス1つ**で、相対パスは**サーバのCWD基準**で解決される。
つまり保存先が「その日サーバをどこで起動したか」で変わり、実際に保存された
ファイルは1つも存在しなかった（機能はあるが使われていない状態）。

## 波及

- ブラウザのネイティブファイルダイアログ（File System Access API）は**使わない**。
  実際にファイルを書いているのはサーバ側のPythonなので、ブラウザがファイルハンドルを
  持っても噛み合わない。自前の一覧UIになる。
- フォルダが決まると、**自動保存**（無題モデルの置き場ができる）と
  **未保存マーク**が初めて成立する。ただし今回のスコープには入れていない。
- サイドパネルを1枚増やすとviewportが狭まるので、実ブラウザ検査が落ちる可能性がある
  （[[ui-restyle-breaks-hardcoded-pick-coordinates]]）。

関連: [[duocad-oplog-embeds-imported-step]] / [[cad-v1-technical-decisions]]
