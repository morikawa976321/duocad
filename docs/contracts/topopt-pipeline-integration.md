# 形状最適化のパッケージ構成・検査・平滑化の契約

形状最適化のパッケージ構成（依存の向き・import の規律・境界検査の免除）、検査コードの罠、メッシュ化の Taubin 平滑化まわりの契約を、ソースを読んでも分からない形で分野別に蒸留した文書。
**いまの挙動の正はコードとテスト**（実測と判断は [../TOPOPT.md](../TOPOPT.md)）。ここは、そこに無い（または散らばっている）不変条件・依存禁止・罠だけを持つ。
出典は項目末尾の括弧内（タスクID, 日付）。ソルバ・性能側の契約は topopt-solver-optimizers.md。

## パッケージ構成と依存の向き（duocad.topopt）

- **呼ぶ側は `from duocad.topopt import design_space as ds` のようにサブモジュールを名指しで import する。** `duocad/topopt/__init__.py` は**サブモジュールを一切 import しない**（`import duocad` は verb 登録で `duocad/__init__.py` から連鎖するので、ここで scipy / skimage / build123d を引き込むと pytest が毎回払う）。`import duocad.topopt` だけではサブモジュールは生えない。`duocad/__init__.py` から `topopt` を import しない。（20260904-1600-duocad-move-topopt-into-core, 2026-09-04）
- この規律の退行を検査するなら見るのは `skimage` と `duocad.topopt` であって scipy ではない（`import duocad.view` で numpy と scipy が来る）。`tests/test_optimize_backend.py` 参照。（20260904-1600-duocad-move-topopt-into-core, 2026-09-04 ※後続の整理で追記された指摘）
- **依存は一方向。** `duocad/topopt/` から検証側（`topopt_checks/` / `web/scripts/`）を import しない（製品が検査の道具に依存すると、道具を捨てるにも直すにも製品が巻き添えになる）。旧実験場 `probe_topopt` への参照も作らない。（20260904-1600-duocad-move-topopt-into-core, 20260905-1701-duocad-remove-probe-topopt, 2026-09-04）
- **`duocad.topopt.OUTPUT_DIR`（= `duocad/topopt/output/`）が成果物（STL・測定JSON）の置き場。** `output/...` と相対パスで書かない（呼び出し側のカレント直下にゴミが落ちる）。`duocad/topopt/output/` に成果物が残っていることに依存しない（追跡は一部だけで、残りは `.gitignore` が外す）。（20260904-1600-duocad-move-topopt-into-core, 2026-09-04）
- **モジュールを素のスクリプトで走らせない。** 相対 import なので `python duocad/topopt/design_space.py` は動かない。`python -m duocad.topopt.<module>` を使う。（20260904-1600-duocad-move-topopt-into-core, 2026-09-04）
- **`tests/test_boundaries.py` の免除は名指しで、ディレクトリ単位ではない**（OCP: `topopt/voxelize.py` / build123d: `topopt/{voxelize,anchor,design_space}.py`）。`duocad/topopt/` に新ファイルを足して OCP か build123d を直接 import すると pytest が落ちる。**免除を増やす前に `duocad.kernel` / `duocad.query` 越しにできないかを考える**（一覧の中身は `test_boundary_scan_covers_every_source_file` が固定しているので、増やすには2箇所直す）。（20260904-1600-duocad-move-topopt-into-core, 2026-09-04）
- **受入検査は `topopt_checks/`（リポジトリ直下。`duocad/` の外）。** `duocad/` の下へ移さない（検査は合成ソリッドを build123d で組むので、中に置くと境界検査に免除が11本ぶん要る。境界の設計意図は「サブパッケージを作って逃げられない」）。各検査は先頭で `import _bootstrap` してリポジトリ直下を `sys.path` へ足す。pytest には入っておらず別途走らせる。件数・本数は時点で動くので契約にしない（検査の項目数を減らさない、という原則だけが契約）。（20260904-1600-duocad-move-topopt-into-core, 2026-09-04）

## probe_topopt の廃止後の参照

2026-09-05 に実験場 `probe_topopt/` を削除した（`70eb180`。中身は `03bf794` までの履歴にある）。撮る道具は `web/scripts/shoot-topopt.mjs`。

- **`docs/TOPOPT.md` は時系列の文書で、古い節は隔離フォルダだった時期の記録。節ごとに日付を見る。**（20260905-1701-duocad-remove-probe-topopt, 2026-09-05）
- 罠: `web/scripts/shoot-topopt.mjs` の `PAGE` テンプレートリテラル内はバッククォート禁止（コメントのコード引用には `'` を使う）。（20260905-1701-duocad-remove-probe-topopt, 2026-09-05）

## 検査コードが共有成果物を上書きする罠

- **成果物を既定の出力先へ書く検査・測定は、書き先を明示する（STL を使わない呼び出しでも）。** 省略すると共有ファイルへ書き、別の回帰成果物を小格子の結果で上書きする（実例: 追跡下の成果物 STL が 47,684→4,684 バイトに上書きされた）。検査を回したら毎回 `git status` を見て、出ていれば `git checkout --` で戻す（自分の成果物ではない）。（20260904-1557-topopt-filter-radius-in-mm, 20260910-1311-topopt-remove-case5, 2026-09-10）
- 罠: `git commit -- <path>` は索引でなく**作業ツリーの内容**を取るので、並行セッションが同じファイルへ入れた未コミット行が同梱される（`README.md` と `docs/TOPOPT.md` に他タスクの文書地図の1行が乗った例）。並行セッションが同じ docs を編集していることがあるので `git status` を見てから触る。（20260910-1311-topopt-remove-case5, 20260905-1528-topopt-find-filter-parameters, 2026-09-10）
- **回帰指紋（ハッシュ）を採る検査は、既定に追随しうる引数を全部明示する。** 省略した呼び出しがあると、既定を動かしたとき指紋が黙って別物になる。「既定を変えていない」を assert する検査は既定を変えるタスクの障害物になるので、守りたい基準を直接 assert する。（20260905-1528-topopt-find-filter-parameters, 2026-09-05）

## メッシュ化の Taubin 平滑化

- **Taubin 平滑化の反復数 `mesh.DEFAULT_ITERS=30`**（`DEFAULT_LAMBDA=0.5` / `DEFAULT_MU=-0.53` / `DEFAULT_LEVEL=0.5`）。Taubin 30回は1セル幅の構造を消すフィルタとして働く。中周波のうねり（k≈0.25〜0.5）を消すのは密度場側のガウシアンの役目で、Taubin の反復数を増やしても効率が悪い（伝達関数 `f(k)`: k=0.25 で 0.991 / 0.5 で 0.949 / 1.0 で 0.765。**途中式は python で計算し直して検算すること**。暗算で 0.983 と間違えた）。一時 12 に下げたが 30 へ戻して現在に至る（20260904-1650 は 12 としたが 20260905-1528 が 30 に戻した）。（20260904-1650-topopt-gaussian-smoothing-in-mesh-pipeline, 20260905-1528-topopt-find-filter-parameters, 2026-09-05）
