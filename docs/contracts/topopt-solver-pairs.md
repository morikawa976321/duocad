# 形状最適化の cg／直接解法の対の測定（solver pairs）の契約

この文書は、実探索の各評価で cg と直接解法（`spsolve`）を対にして測り、「cg と直接解法の使い分け条件」の材料を集めた一連の測定（hanger 2.4mm・2.0mm・4.8mm、合成の塊 S1〜S3、荷重 200 N の hanger）について、
**ソースを読んでも分からない不変条件・依存禁止・踏んだ罠・読み方の限界**を、元の作業タスクの契約から分野別に蒸留したもの。
いまの挙動の正はコードとテスト、測定結果・表・決定の正は [../TOPOPT_SPEED.md](../TOPOPT_SPEED.md)「cg と直接解法の使い分け条件の材料（実探索の対の測定）」と「決定（2026-09-22 本人）: cg と直接解法の使い分け」。
各項目末尾の括弧は出典 `(タスクID, 日付)`。

**現行（コードで確認済み）**: 測定の結論は製品に入った。`duocad/topopt/fem.py` の `solver="auto"` は、縮めた K_ff の自由な自由度が `AUTO_DIRECT_MAX_DOFS = 50_000` 以下かつ nnz が `AUTO_DIRECT_MAX_NNZ_PER_DOF = 60.0` × 自由度以下なら直接解法、それ以外は cg を選び、探索と6条件の既定になっている（`bcd212b`、本人判断 2026-09-22）。
測定スクリプト `topopt_checks/measure_solver_pairs.py`（と `_hanger_setup.py`・`measure_inner_target_synthetic.py`）は `312aac5`（一度きりの測定スクリプト17本の削除）で消えた。生データ `output/solver_pairs/` は git 管理外。
以下の「呼ぶ面」はそのスクリプトのもので、**再現するなら `312aac5` の親コミットから復元する**（現リポジトリに無い `(対象コード不在)`）。

## 測定スクリプトの面と不変条件（measure_solver_pairs.py。削除済み）

- 起動: `.\.venv\Scripts\python.exe topopt_checks\measure_solver_pairs.py --subject hanger|S1|S2|S3 --spacing S --target T --cells M --out J.jsonl [--save-accepted DIR] --head SHA [--max-evals 300(上限900)] [--time-limit-min 150] [--min-free-gb 12] [--pair-limit 130000] [--pair-min-free-gb 8]`。
  `--target` は**探索に渡す目標そのもの**（製品の既定 = 2.0 × 2.4 = 4.8。`_setup` の係数 1.0 固定はスクリプトが `dataclasses.replace` で差し替え、渡った値 == `--target` を起動時と探索の入口で assert）。
  `--time-limit-min` は探索の開始（段階3の前）からの壁時計で、対の測定の時間を含む。`--max-evals` は段階4の評価の数（幾何却下・FEM なしを含む）。`--out` が既にあれば拒否。起動時の空きメモリが `--min-free-gb` 未満なら終了コード 3。 (20260921-2305-topopt-solver-pairs-script-e24, 2026-09-21)
- 対を測るのは、cg で解いた solve のうち、右辺が1本・非零・自由な自由度が `--pair-limit` 以下・直前の空きメモリが `--pair-min-free-gb` 以上のとき。条件を満たさなければ `pair.status='skipped'` と理由（`dofs_above_limit`・`low_memory`・`multi_rhs_or_zero_rhs`）。 (20260921-2305-topopt-solver-pairs-script-e24, 2026-09-21)
- jsonl: 1行目がヘッダ（`head`・`label`・`conditions_target_safety_factor`・`min_width_cells`・`grid_shape`・`design_cells`・`cg_rtol`・`pair_limit`・`duocad_root`・`stop_reason`・`termination`・`stage3_seconds`・`stage4_seconds`・`peak_working_set_gb`・`record_count_matches`・`final_*` ほか）、以降が `event:"eval"`: `n`・`stage`(3|4)・`operation`・`removed`・`parent_cells`・`cells`・`outcome`・`failed`・`safety_factor`・`t_s`・`cpu_percent_{system,self,other}`・`solves[]`（`solver`・`free_dofs`・`material_cells`・`nnz`・`nnz_per_dof`・`bbox_cells`・`fill_ratio`・`seconds`・`iterations`・`fallbacks`・`pair`〈`status`・`seconds`・`rss_increase_gb`・`relative_residual`・`finite`・`warnings`・`max_relative_difference`・`overhead_seconds`・`rcm{seconds,bandwidth_max,profile,sum_sq_bandwidth}`〉）。 (20260921-2305-topopt-solver-pairs-script-e24, 2026-09-21)
- **探索は cg のまま進む**（直接解法の解は測ったら捨てる。軌道は製品と同じ）。`--self-check`（約4分。4.8mm・目標 2.0・`--pair-limit 50000`）が、包まない探索と包んだ探索の cg 反復数の列・評価ごとの（合否・セル数・安全率）・採用形状のハッシュ・評価数の一致を固定していた。合成題材の自己検査は追加していない。 (20260921-2305-topopt-solver-pairs-script-e24, 2026-09-21; 20260921-2307-topopt-solver-pairs-fat-shapes, 2026-09-21)
- 段階3の solve も記録するが、表A〜D は段階4だけ。`accepted_000.npz` は段階3の全材料の形（段階4の出発形）、`accepted_001.npz` 以降が段階4の合格の通し番号（`mask`・`origin`・`spacing`）。秒は `PreparedSystem.solve` 1回（組立・縮約を含まない）で、cg の秒は残差検算を含み、direct の秒は `spsolve` だけ。 (20260921-2305-topopt-solver-pairs-script-e24, 2026-09-21)
- `--analyze J...`（表A〜D と対の一覧。題材・格子・目標・m が同じ記録は1つの表にまとまり、複数あれば最後に全体の表。出力は UTF-8 に再設定済み）・`--spacing S --shape-quantities NPZ...`（既存の形に自由度・nnz・充填率・RCM の帯幅を当てる。探索も直接解法も走らせない）。 (20260921-2305-topopt-solver-pairs-script-e24, 2026-09-21)
- **`--subject` の足し方**: `SUBJECTS[名前] = 関数(args) -> Subject(label, mask, conditions, spacing, origin, extra)`。探索・記録・解析は `Subject` しか見ない。`conditions.target_safety_factor` は `args.target` と一致させる（`build_subject` が assert）。`label`・`mask.shape`・`args.target`・`args.cells` が `--analyze` の集計キー。
  `main()` は `--spacing` を hanger だけの必須にしている（`SUBJECTS_REQUIRE_SPACING = {'hanger'}`）。 (20260921-2305-topopt-solver-pairs-script-e24, 2026-09-21; 20260921-2307-topopt-solver-pairs-fat-shapes, 2026-09-21)
- 合成題材 `S1`・`S2`・`S3`（`SUBJECTS['S1'|'S2'|'S3'] = _build_synthetic(名前)`）: 設計空間は全材料（`np.ones(SYNTHETIC_SHAPE, dtype=bool)`。80×20×20）から始め、条件は `measure_inner_target_synthetic.build_conditions(name, 1, args.target)` をそのまま呼ぶ（コピーしない）。`label` は `synthetic-{name}`。 (20260921-2307-topopt-solver-pairs-fat-shapes, 2026-09-21)
- **`--model <models/ 内のファイル名>`**（既定 `hanger_bar_attachment.json`）: `_build_hanger` が `_setup(args.spacing, model_name=args.model)` を呼び、`Subject.extra` に `model_file`・`model_sha1` を足す（ヘッダに自動で入る）。`label` は既定モデルなら今までどおり `hanger-e{spacing:g}`、それ以外は語幹 `hanger_bar_attachment` を取り除いた残りで `hanger{タグ}-e{spacing:g}`（200N版は `hanger200n-e2.4`）。`self_check` は冒頭で `args.model = DEFAULT_HANGER_MODEL` を代入する。
  `--model` を渡さない既存の呼び出しは label・集計キーとも無変更。`model_file`・`model_sha1` は hanger だけに付き（S1〜S3 の `extra` には無い）、`--analyze` はこの2キーを参照しないので、この欄が無い旧 jsonl も読める。`_model_tag` は既定と200N版の2ファイルでしか確認していない（拡張子なし指定は `Session.open_document()` 側が `.json` を要求して別のエラーになる）。 (20260922-1515-topopt-solver-pairs-hanger-200n, 2026-09-22)
- ヘッダの `label` は 2.0mm の hanger で `hanger-e2`（`{spacing:g}` 整形で 2.0 が 2 になる）。`grid_shape` [82,155,120]・`design_cells` 640,306・`actual_spacing_mm` 1.9974 で 2.4mm と区別する。 (20260921-2306-topopt-solver-pairs-e20, 2026-09-21)
- jsonl・`accepted_*.npz` は書き換えない（再測定するなら別名で。`--out` が既にあれば拒否される）。欄の意味を変えたら docs に書く。 (20260921-2306-topopt-solver-pairs-e20, 2026-09-21)
- **表E／表E′（`table_e(groups)`）**: 母集団は「対の実測（`pair.status=='measured'`）がある全評価」を全 jsonl から合わせたもの。5規則（自由度・nnz・自由度+nnz/自由度・自由度+充填率・RCM帯幅²）を、規則1・3・4の自由度閾値 T は固定6値（2・3・5・7・9・11万）、規則2・3の r・4の φ・5の B はその母集団で観測された値そのものを候補にして総当たり（`_rule_candidates`）。
  「損の最大が1秒以下の中で秒の合計が最小」の組を選び（`_select_best`）、**候補の総当たり列の元の順（秒の合計順ではない）**でその前後を表に出す（`_rule_rows_markdown`）。規則5は RCM の計算の秒を費用として全体の秒に足す。表E′は表Eを書き換えず、その下に追加した。 (20260921-2307-topopt-solver-pairs-fat-shapes, 2026-09-21; 20260922-1515-topopt-solver-pairs-hanger-200n, 2026-09-22)
- **表Eの候補値は丸めない**（生の `nnz_per_dof`・`fill_ratio`・`nnz`・`sum_sq_bandwidth` を候補にする）。丸めてから比較すると、丸めが切り捨て方向に働いた行が自分自身の観測値から作った閾値を満たせなくなる（6桁丸めだと221対中 `nnz_per_dof` で96対・`fill_ratio` で70対が自己除外される）。表示の書式は比較に使わない値にだけ適用する。 (20260921-2307-topopt-solver-pairs-fat-shapes, 2026-09-21)
- 表Eの候補格子（N・r・φ・B の値）は観測値から作る（221対、表E′は315対）。別の題材・探索を足して母集団が変われば候補格子ごと変わる。「前後」は秒の合計の昇順ではない（QA で実装に合わせて docs を修正した）。 (20260921-2307-topopt-solver-pairs-fat-shapes, 2026-09-21; 20260922-1515-topopt-solver-pairs-hanger-200n, 2026-09-22)

## 測定結果の要点（5000 N 模型の hanger と合成の塊）

**注意（荷重）**: 20260921-2305・2306・2307 の hanger の測定は、`models/hanger_bar_attachment.json` の荷重が意図して **5000 N** になっていた模型での値（`docs/TOPOPT_TARGET_FACTOR.md` §7.5。製品の既定の軌道は 200 N、下の「hanger 200 N」）。
該当するのは 2305 の hanger 2.4mm（T48/T20）・4.8mm（`hanger-e48-t20-m2-pl50k`）、2306 の 2.0mm、2307 の hanger 4.8mm 目標 4.8（20260922-1515 の契約はこれらと S1〜S3 を「5000 N の記録」と呼ぶ。S1〜S3 の力そのものは 2N/20N/2N の合成題材で hanger の荷重とは別。荷重の扱いは 1515 の契約の言い方を引き継いだもので、各記録の模型の荷重は `model_sha1` の無い旧 jsonl では確認できない `(要確認)`）。

- **hanger 2.4mm・探索に渡す目標 4.8・m=2 は対が 0**: 評価 10 回・14 分で `within_target_tolerance`、239,763 セル（設計空間の 63.6%）、全評価が自由度 778,959 以上で `--pair-limit` 13万以下の評価が無い。目標 2.0 でも 17 評価・62,030 セル（232,251 自由度）で止まり対は 0。ピーク常駐 7.8GB、開始時の空きメモリ 21GB。 (20260921-2305-topopt-solver-pairs-script-e24, 2026-09-21)
- **hanger 2.0mm・目標 4.8・m=2 も対が 0**: 評価 11 回（段階3が1・段階4が10。すべて合格）・28.3 分で `within_target_tolerance` に収束、全評価が自由度 1,261,467 以上（`--pair-limit` 13万の 9.7 倍以上）、最終 391,703 セル（設計空間の 61.2%）・安全率 4.8388。2.0mm の cg の 1 solve は 107〜166 秒（2.4mm の平均の約 1.9 倍。反復 1.20 倍・1 反復の秒 1.5 倍）、ピーク常駐 13.0GB。段階3の評価の材料は 640,298 セルで設計空間 640,306 より 8 少ない（2.4mm も 377,067 対 377,075 で同じ 8 セル差。理由は未調査）。 (20260921-2306-topopt-solver-pairs-e20, 2026-09-21)
- 4.8mm・目標 2.0 の細い側の対 11 対（5 万自由度以下で 2.4〜11.5 秒・+0.3〜0.9GB）が、4.8mm で取れた唯一の対。4.8mm・目標 4.8 は 10 評価・自由度 102,840〜157,815 で、`--pair-limit 80000` 以下では対が 0、13万では太い形の直接解法が終わらない恐れ。 (20260921-2305-topopt-solver-pairs-script-e24, 2026-09-21; 20260921-2307-topopt-solver-pairs-fat-shapes, 2026-09-21)
- **太い形の直接解法は終わらないことがある**: 4.8mm・目標 4.8 の n=3（材料 36,611 セル・125,874 自由度）で、`--pair-limit` 13万の自己検査が対の測定の途中で 8 分待っても終わらなかった（cg は 4 秒）。`spsolve` は途中で止められず、スクリプトに時間の上限は無い。**初めての格子・題材は `--pair-limit` を下げて始める。** (20260921-2305-topopt-solver-pairs-script-e24, 2026-09-21)
- **合成の塊は、自由度が同程度でも hanger の形より direct が重い**: S2 の 87,144 自由度（充填率 0.800・全材料に近い形）は direct 156.16 秒・+5.62GB、同程度の hanger 4.8mm の対（49,587 自由度・充填率 0.338）は 11.47 秒・+0.86GB。S3 の 89,298 自由度は direct 246.25 秒・+5.71GB。自由度 7〜9 万で direct が cg の 47〜249 倍遅い評価が出た。
  S1 22 対・S2 20 対・S3 168 対（自由度 3,852〜89,298。全設計空間 80×20×20・目標 4.2・m=2、`--pair-limit` は S1=60000・S2/S3=90000・hanger-e48-t48=80000）。**`--pair-limit` を上げるときは、その題材・その序盤の形（充填率が高い）で実際に測ってから**（100,000 超は未試験）。 (20260921-2307-topopt-solver-pairs-fat-shapes, 2026-09-21)
- 0538（節O）の収束形・倍率はこの測定に引き継がれていない: `--max-evals 200` で S2・S3 は 0538 より安全率が高い側（材料が少ない側）まで削れて `no_removable_cell` に止まった（S1 は `within_target_tolerance` で 0538 の O1 に近い。S2 は目標の 1.383 倍・S3 は 1.0224 倍、0538 の O1 は S1・S2 が 1.0001〜1.0044 倍）。0538 から使ったのは「目標 4.2・m=2 が収束する組」という選定根拠だけ。 (20260921-2307-topopt-solver-pairs-fat-shapes, 2026-09-21)
- 全評価の (自由度・nnz・nnz/自由度・充填率・cg の秒) は 2.4mm・2.0mm とも全 solve にあるが、対が無いので `pair.rcm`（RCM の帯幅）・`direct` の秒は無い。**「量が無い」と書く。0 と書かない。** 2.4mm・2.0mm の記録は規則1〜4で「cg のまま」の評価として数えられるが、規則5（RCM）には当てられず、「対の実測がある全評価」の母集団には入らない。 (20260921-2306-topopt-solver-pairs-e20, 2026-09-21)

## hanger 200 N（製品の既定の軌道）

- 200 N の模型で製品の既定（2.4mm・目標 4.8・m=2）を1本走らせたところ、**自由度 13 万以下に初めて実部品の探索軌道の対が 94 対取れた**（自由度 32,508〜115,563）。94 対のうち 93 対で direct のほうが cg より速かった（比 0.08〜0.60）。5000 N の記録（hanger 4.8mm・S1〜S3）は同程度以上の自由度で direct が 2.2〜249 倍遅い評価ばかりだったので、**この軌道は向きが逆**（機序は測っていない: 荷重差・軌道差の切り分けなし）。 (20260922-1515-topopt-solver-pairs-hanger-200n, 2026-09-22)
- 表E（221対）にこの94対を足した表E′（315対）で、規則1（自由度だけ）は hanger200n を1件も捕捉できず（取りこぼしが 3 件→96 件・3.1 秒→658.0 秒に悪化）、規則2〜5（nnz・nnz/自由度・充填率・RCM帯幅を自由度と併用）は 87〜93 件を捕捉した（hanger200n の nnz/自由度が約 53 で、合成の塊 S2/S3 の約 74〜81 より低い）。閾値・規則の採否はこの測定自体では判断していない（その後 `solver="auto"` に決まった。上記「現行」）。 (20260922-1515-topopt-solver-pairs-hanger-200n, 2026-09-22)
- 依存しない: **秒**（`cpu_percent_other` 中央値 13.7%・最大 18.7% で既存8本〈中央値 2.4〜3.3%・最大 5.8%〉より並走の影響が強い可能性。原因のプロセスは特定していない）。自由度・nnz・帯幅・セル数・安全率は決定的。対は 200N・2.4mm・目標 4.8・m=2 の1条件だけ（各条件1本・再走なし。200N での 2.0mm・4.8mm や m=3 は測っていない）。 (20260922-1515-topopt-solver-pairs-hanger-200n, 2026-09-22)
- 測定コマンドの完全な形は docs の「再現」: `--subject hanger --model hanger_bar_attachment_200n.json --spacing 2.4 --target 4.8 --cells 2 --max-evals 300 --time-limit-min 180 --min-free-gb 12 --pair-limit 130000 --pair-min-free-gb 8`。 (20260922-1515-topopt-solver-pairs-hanger-200n, 2026-09-22)

## 依存してはいけないもの・読み方の限界（共通）

- **秒は参考値**（並走・日・機械で変わる）。`cpu_percent_other` は並走の目安。反復数・自由度・nnz・帯幅・セル数・安全率は決定的。表Eの「秒の合計」「損」「取りこぼし」もすべて参考値。 (20260921-2305-topopt-solver-pairs-script-e24, 2026-09-21; 20260921-2307-topopt-solver-pairs-fat-shapes, 2026-09-21)
- **探索の軌道**（評価数・止まり方・自由度の範囲）は当時の探索コード（`target_tolerance` 0.01 を含む）の結果で、別の版に当てはめない。`docs/TOPOPT_SPEED.md` の前の節の交差点（約 11 万自由度）は試作の projected peel が通った形での値で、製品の探索の軌道には出てこない。 (20260921-2305-topopt-solver-pairs-script-e24, 2026-09-21)
- 2.0mm・2.4mm の cg/direct の秒の比・交差点の自由度は、対が 0 の記録からは何も読めない。条件を変えて（目標を下げる等）対を取りにいく測定は当時していない。 (20260921-2306-topopt-solver-pairs-e20, 2026-09-21)
- `--head` は記録用の文字列で、走らせたソースは検証しない（`duocad_root` がヘッダに入る。スクリプトは `fem.__file__` が隣のソースでなければ終了する）。T48 の記録（`0b86771` の版）は `overhead_seconds` に gc の時間を含まないので、対のある記録は `0a2608d` 以降の版で取る。 (20260921-2305-topopt-solver-pairs-script-e24, 2026-09-21)
- 実行中の空きメモリ（21.1〜21.7GB）は標準出力の `free=` 表示にだけあり jsonl には無い（ヘッダに開始時・終了時）。 (20260921-2306-topopt-solver-pairs-e20, 2026-09-21)

## 運用の罠

- venv の python を `Start-Process` で起動すると launcher（常駐 0GB）の子に本体が走る。`Wait-Process -Id <launcher>` で待てる。**PowerShell ツールの既定タイムアウトは 120 秒**で、`Wait-Process -Timeout 120` はそれに当たってバックグラウンドへ回される。待つときはツールの `timeout` に 600000 を指定し、`Wait-Process` は 500〜540 秒にする。bash の `sleep` は禁止される。 (20260921-2305-topopt-solver-pairs-script-e24, 2026-09-21; 20260921-2306-topopt-solver-pairs-e20, 2026-09-21)
- bash の heredoc で日本語を含む Python を書くと cp932 で読まれて文字化けする（日本語のあるファイルの編集は Edit ツール）。cursor-agent の出力 JSON を `python -c` で `print()` して端末へ出すと console codepage で文字化けするので、`open(..., 'w', encoding='utf-8')` でファイルへ落としてから Read で読む。 (20260921-2305-topopt-solver-pairs-script-e24, 2026-09-21; 20260921-2307-topopt-solver-pairs-fat-shapes, 2026-09-21)
- `git archive HEAD duocad topopt_checks` を一時フォルダへ展開して回すとき、`.venv` は一時フォルダに無いので、本体チェックアウトの `.venv/Scripts/python.exe` を絶対パスで呼び、スクリプトだけ一時フォルダのものを渡す（`_bootstrap.py` が一時フォルダのルートを `sys.path` に挿す）。本体の `duocad/` を触っていないことを都度 `git status --short -- duocad topopt_checks` で確認する。 (20260921-2307-topopt-solver-pairs-fat-shapes, 2026-09-21)
- コメント（docstring 含む）に taskbox のタスクID（「0538」「2307の契約」）を書くと CLAUDE.md のコメント規律に外れる。コード中の参照は関数名・ファイル名にする。docs の言葉は実装を先に読んでから選ぶ（「昇順に並べた列の前後」のような具体的な順序の主張は、コードを見ずに書かない）。複数規則の差分をまとめて書くときは、「全部変わった／全部同じ」の一般化を先に書かず、規則ごとの実際の値を確認してから要約する（QA が自己矛盾を指摘した）。 (20260921-2307-topopt-solver-pairs-fat-shapes, 2026-09-21; 20260922-1515-topopt-solver-pairs-hanger-200n, 2026-09-22)
- cursor に委譲するとき、bash のリダイレクト先をバックスラッシュ区切りのまま（`> C:\Users\...\out.json`）渡すと、bash が `\` をエスケープ文字として食い、パス区切りの無い 1 つのファイル名がカレントディレクトリに作られる。**リダイレクト先は必ずフォワードスラッシュ（`/c/Users/...`）で書く**（ダブルクォート内の引数は `\` のままでよい）。 (20260922-1515-topopt-solver-pairs-hanger-200n, 2026-09-22)
- 測定の起動直後に他セッションが `duocad/topopt/finalize.py` を未コミットで変更し始めた例がある。着手時と終了時に `git status --short -- duocad` で自分のコミットに混入していないことを確認する。 (20260922-1515-topopt-solver-pairs-hanger-200n, 2026-09-22)
