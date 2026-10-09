# 形状最適化の cg／直接解法の使い分け（solver pairs）の契約

この文書は、実探索の各評価で cg と直接解法（`spsolve`）を対にして測り、「cg と直接解法の使い分け条件」の材料を集めた一連の測定（hanger 2.4mm・2.0mm・4.8mm、合成の塊 S1〜S3、荷重 200 N の hanger）について、
**測定結果を読むときの限界と、測定・運用で踏んだ罠**を、元の作業タスクの契約から分野別に蒸留したもの。
いまの挙動の正はコードとテスト、測定結果・表・決定の正は [../TOPOPT_SPEED.md](../TOPOPT_SPEED.md)「cg と直接解法の使い分け条件の材料（実探索の対の測定）」と「決定（2026-09-22 本人）: cg と直接解法の使い分け」。
各項目末尾の括弧は出典 `(タスクID, 日付)`。

**現行（コードで確認済み）**: 測定の結論は製品に入った。`duocad/topopt/fem.py` の `solver="auto"` は、縮めた K_ff の自由な自由度が `AUTO_DIRECT_MAX_DOFS = 50_000` 以下かつ nnz が `AUTO_DIRECT_MAX_NNZ_PER_DOF = 60.0` × 自由度以下なら直接解法、それ以外は cg を選び、探索と6条件の既定になっている（`bcd212b`、本人判断 2026-09-22。テストは `tests/test_solver_auto.py`）。
測定スクリプトは一度きりの調査用で削除済み。生データ `output/solver_pairs/` は git 管理外。

## 測定結果の読み方の限界

- **荷重の罠**: hanger 2.4mm（T48/T20）・2.0mm・4.8mm の測定は、`models/hanger_bar_attachment.json` の荷重が意図して **5000 N** になっていた模型での値（`docs/TOPOPT_TARGET_FACTOR.md` §7.5。製品の既定の軌道は 200 N の `hanger_bar_attachment_200n.json`）。
  S1〜S3 の力は 2N/20N/2N の合成題材で hanger の荷重とは別だが、記録の分類では「5000 N の記録」側に数える。各記録の模型の荷重は `model_sha1` の無い旧 jsonl では確認できない `(要確認)`。
  (20260921-2305-topopt-solver-pairs-script-e24, 2026-09-21; 20260922-1515-topopt-solver-pairs-hanger-200n, 2026-09-22)
- **200 N の軌道は 5000 N の記録と direct と cg の向きが逆**: 200 N の模型で製品の既定（2.4mm・目標 4.8・m=2）を1本走らせると、自由度 32,508〜115,563 の実部品の探索軌道の対が 94 対取れ、93 対で direct のほうが cg より速かった（比 0.08〜0.60）。
  5000 N の記録（hanger 4.8mm・S1〜S3）は同程度以上の自由度で direct が 2.2〜249 倍遅い評価ばかりだった。機序は測っていない（荷重差・軌道差の切り分けなし）。対は 200N・2.4mm・目標 4.8・m=2 の1条件だけで、各条件1本・再走なし。
  (20260922-1515-topopt-solver-pairs-hanger-200n, 2026-09-22)
- **太い形の直接解法は終わらないことがある**: 4.8mm・目標 4.8 の n=3（材料 36,611 セル・125,874 自由度）で、対の測定の途中で `spsolve` が 8 分待っても終わらなかった（cg は 4 秒）。`spsolve` は途中で止められない。
  同じ自由度でも充填率の高い形（合成の塊 S2/S3）は hanger の形より direct が桁違いに重い。**初めての格子・題材で direct を回すときは自由度の上限を低く始め、その題材・その序盤の形で実際に測ってから上げる。** (20260921-2305-topopt-solver-pairs-script-e24, 2026-09-21; 20260921-2307-topopt-solver-pairs-fat-shapes, 2026-09-21)
- **秒は参考値**（並走・日・機械で変わる）。反復数・自由度・nnz・帯幅・セル数・安全率は決定的。 (20260921-2305-topopt-solver-pairs-script-e24, 2026-09-21; 20260922-1515-topopt-solver-pairs-hanger-200n, 2026-09-22)
- **探索の軌道**（評価数・止まり方・自由度の範囲）は当時の探索コード（`target_tolerance` 0.01 を含む）の結果で、別の版に当てはめない。`docs/TOPOPT_SPEED.md` の前の節の交差点（約 11 万自由度）は試作の projected peel が通った形での値で、製品の探索の軌道には出てこない。 (20260921-2305-topopt-solver-pairs-script-e24, 2026-09-21)
- 全評価の（自由度・nnz・nnz/自由度・充填率・cg の秒）は 2.4mm・2.0mm とも全 solve にあるが、対が無いので RCM の帯幅・direct の秒は無い。**「量が無い」と書く。0 と書かない。** (20260921-2306-topopt-solver-pairs-e20, 2026-09-21)

## 運用の罠

- venv の python を `Start-Process` で起動すると launcher（常駐 0GB）の子に本体が走る。`Wait-Process -Id <launcher>` で待てる。**PowerShell ツールの既定タイムアウトは 120 秒**で、`Wait-Process -Timeout 120` はそれに当たってバックグラウンドへ回される。待つときはツールの `timeout` に 600000 を指定し、`Wait-Process` は 500〜540 秒にする。bash の `sleep` は禁止される。 (20260921-2305-topopt-solver-pairs-script-e24, 2026-09-21; 20260921-2306-topopt-solver-pairs-e20, 2026-09-21)
- bash の heredoc で日本語を含む Python を書くと cp932 で読まれて文字化けする（日本語のあるファイルの編集は Edit ツール）。JSON を `python -c` で `print()` して端末へ出すと console codepage で文字化けするので、`open(..., 'w', encoding='utf-8')` でファイルへ落としてから Read で読む。 (20260921-2305-topopt-solver-pairs-script-e24, 2026-09-21; 20260921-2307-topopt-solver-pairs-fat-shapes, 2026-09-21)
- `git archive HEAD duocad topopt_checks` を一時フォルダへ展開して回すとき、`.venv` は一時フォルダに無いので、本体チェックアウトの `.venv/Scripts/python.exe` を絶対パスで呼び、スクリプトだけ一時フォルダのものを渡す（`_bootstrap.py` が一時フォルダのルートを `sys.path` に挿す）。本体の `duocad/` を触っていないことを都度 `git status --short -- duocad topopt_checks` で確認する。 (20260921-2307-topopt-solver-pairs-fat-shapes, 2026-09-21)
- docs の言葉は実装を先に読んでから選ぶ（「昇順に並べた列の前後」のような具体的な順序の主張は、コードを見ずに書かない）。複数規則の差分をまとめて書くときは、「全部変わった／全部同じ」の一般化を先に書かず、規則ごとの実際の値を確認してから要約する（QA が自己矛盾を指摘した）。 (20260921-2307-topopt-solver-pairs-fat-shapes, 2026-09-21; 20260922-1515-topopt-solver-pairs-hanger-200n, 2026-09-22)
- bash のリダイレクト先をバックスラッシュ区切りのまま（`> C:\Users\...\out.json`）渡すと、bash が `\` をエスケープ文字として食い、パス区切りの無い 1 つのファイル名がカレントディレクトリに作られる。**リダイレクト先は必ずフォワードスラッシュ（`/c/Users/...`）で書く**（ダブルクォート内の引数は `\` のままでよい）。 (20260922-1515-topopt-solver-pairs-hanger-200n, 2026-09-22)
- 測定の起動直後に他セッションが `duocad/topopt/finalize.py` を未コミットで変更し始めた例がある。着手時と終了時に `git status --short -- duocad` で自分のコミットに混入していないことを確認する。 (20260922-1515-topopt-solver-pairs-hanger-200n, 2026-09-22)
