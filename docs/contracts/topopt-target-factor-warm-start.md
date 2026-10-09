# 形状最適化の探索時の目標係数・内側目標・warm start（撤去済み）の契約

この文書は、(1) 探索に渡す目標安全率 = 実際の目標 × 係数 2.4 の配線（`SEARCH_TARGET_FACTOR`）、(2) 内側の目標（探索に渡す目標を上げて細分で目標を満たすかを見る）の測定の読み方、
(3) CG の warm start（前の形の変位を初期値にする）を**導入してから撤去した経緯の現行の姿**について、
**ソースを読んでも分からない不変条件・依存禁止・踏んだ罠**を、元の作業タスクの契約から分野別に蒸留したもの。
いまの挙動の正はコードとテスト。決定・根拠・実測の正は [../TOPOPT_TARGET_FACTOR.md](../TOPOPT_TARGET_FACTOR.md)（§1 決定と配線〜§7 配線後の確認）、[../TOPOPT_THICKNESS.md](../TOPOPT_THICKNESS.md) 節N・O、[../TOPOPT_SPEED.md](../TOPOPT_SPEED.md)。
各項目末尾の括弧は出典 `(タスクID, 日付)`。

## 探索時の目標係数（optimize_run.SEARCH_TARGET_FACTOR = 2.4）

- **口**: `optimize_run.SEARCH_TARGET_FACTOR = 2.4`（定数）と `OptimizeOptions.search_target_factor`（既定は定数）。製品の既定の経路は `OptimizeOptions(target_safety_factor=2.0, ...)` のままで係数が掛かる。
  **掛けるのは `_build_search_conditions`（`SearchConditions.target_safety_factor` を作る唯一の場所）の1か所だけ**（現行は `_search_target_safety_factor(opts)` が係数の検査と積を持つ）。`binary_search`・`acceptance`・`stress`・`fem` は係数を知らない。`session.py`・`net/ws.py` で掛けない（二重に掛かる）。
  **1.0 に固定したいとき**（テスト・測定）は `OptimizeOptions(..., search_target_factor=1.0)`。1.0 未満・非有限・積が inf は `OptimizeInputError`。 (20260921-1700-topopt-search-target-factor, 2026-09-21)
- 係数は m に依らず1つ・2.4（本人の決定）。UI・WebSocket・`Session.run_optimization` の入力には出さない。2.4 は最小幅 m≥2 セルの直線部材で、探索の格子が曲げ・ねじりを要素中心の応力から見落として安全率を過大に出す上限（m=2 の曲げ 2.36）の切り上げ。 (20260921-1700-topopt-search-target-factor, 2026-09-21)
- **報告の項目名**（`run()` の結果 dict と trace の `resolved-conditions`）: `target_safety_factor`（**実際の目標**。2.0 のまま）・`search_target_safety_factor`（探索時の目標 = 4.8）・`search_target_factor`（2.4）。`safety_factor` は探索の格子での値。
  **利用者に見える「目標」を探索時の目標に置き換えない。安全率の数値を係数で割って見せない**（測っていない数字になる）。 (20260921-1700-topopt-search-target-factor, 2026-09-21)
- 進行メッセージ（`phase="search"`）は「目標安全率 2（探索の格子では 4.8 = 2 × 2.4 を満たす形を探す）」。**進行メッセージ・警告の文面をパースしない**（テストは部分一致で固定しているだけ）。 (20260921-1700-topopt-search-target-factor, 2026-09-21)
  （旧契約の「m=1 の警告（探索時の目標に掛けた係数 2.4 の根拠が成り立たない…）」は、既定の最小幅変更（`_resolve_min_width` が m≥2 を返す）で `_search_target_warning` ごと削除された。 (20260922-1516-topopt-min-width-default, 2026-09-22) → topopt-min-width-peel.md）
- **結果の `safety_factor` を「実際の目標に対する安全率」と読まない**。探索の格子での値で、探索時の目標（4.8）に対する合否。実際の安全率は細分して解き直さないと分からない（倍率は `docs/TOPOPT_TARGET_FACTOR.md` §2 の表）。
  探索の診断・6条件の `checks['safety']['target_safety_factor']` も探索時の目標が入っている（`_full_material_failure_message` は「探索の格子で必要な安全率 4.8（= 目標 2 × 係数 2.4）」と言い換える）。 (20260921-1700-topopt-search-target-factor, 2026-09-21)
- **測定スクリプトの `--target` の意味は変えていない**: `measure_min_width_floor.py --target T` は探索にそのまま渡す目標（`search_target_factor=1.0` で走る）。**製品の既定（実際の目標 2.0 × 2.4）と同じ探索にするには `--target 4.8`**（`2.0 × 2.4` は浮動小数で `4.8` と等しい。2倍は正確）。
  `--element-size`（要素サイズ要求 mm。既定 2.4）も同スクリプトにある。既定のままなら出力は変わらない。探索が受け取る目標が 4.2 のまま（二重に掛からない）ことは `find_best_feasible` を差し替えて確かめてある。 (20260921-1700-topopt-search-target-factor, 2026-09-21; 20260921-0537-topopt-inner-target-hanger-curve, 2026-09-21)
- 既存テスト（pytest 全体）は 1 本も直さずに通った（探索を fake にしたものは係数を見ず、実探索を回すテストが無いため）。実探索を通す `topopt_checks/check_optimize_wiring.py` は「探索時の目標を満たす」に直して通した。 (20260921-1700-topopt-search-target-factor, 2026-09-21)
- 対話セッションの README の丸めが生データとずれた箇所があった（板・曲げ m=2 の 2.31 は生データの逆数 2.316 など）ので、文書は生データから作り直した。回し直した値（`member_probe` 38ケース・`locking_probe`・`thickness_along_search`）は生データとビット単位で一致した。 (20260921-1700-topopt-search-target-factor, 2026-09-21)
- `docs/TOPOPT_TARGET_FACTOR.md` の末尾が「§7 配線後の確認」になっている。`duocad/optimize_run.py` は `w/crlf`、`docs/TOPOPT_THICKNESS.md` は LF のことがある。書き換える前に `git ls-files --eol` で行末を確かめ、Python なら `newline=''` で読み書きする。 (20260921-1700-topopt-search-target-factor, 2026-09-21)

## 配線後の確認（§7）の読み方

- **「hanger が探索の格子で 4.8 を満たし、細分後も 2.0 を上回る」は 5000 N 模型では確かめられなかった。** 4.8 の形は 237,375 要素（設計空間の 63%）で、細分は k=1 のみ（安全率 4.8000）。k=2 は未測定。 (20260921-1701-topopt-search-target-factor-acceptance, 2026-09-21)
- **荷重の罠**: §7.1〜7.4 の測定は `models/hanger_bar_attachment.json`（git 管理外）の荷重が意図せず **5000 N** になっていた模型での値で、節N・K・J の測定は 200 N。比は 25.00（全材料の安全率 164.69 対 6.587）。
  §7.3〜7.5 の値は 5000 N の hanger のもので、係数 2.4 の確認にはなっていない。本人が 5000 N を意図して変えていたため、`hanger_bar_attachment_200n.json`（コピー）で測り直した（§7.8: m=2・目標 4.8 は 5,889 要素〈1.56%〉、m=3 は 7,869 要素〈2.09%〉）。
  **hanger の探索結果を読むときは、どの荷重の模型かを必ず確かめる。** (20260921-1701-topopt-search-target-factor-acceptance, 2026-09-21; 20260922-1515-topopt-solver-pairs-hanger-200n, 2026-09-22)
- 測定のコードは `a0a4a9c`（`git archive` の展開）で、節N（`fb7d0fb`）の列とは版が違う。**並べて差を読まない**（差の原因は切り分けていない）。目標 4.8 の探索は予算 60 の前に `converged`（`safety_at_single_cell`）で止まったので予算 120 の回し直しはしていない。 (20260921-1701-topopt-search-target-factor-acceptance, 2026-09-21)
- 探索の秒は並走の python で汚れている（反復数・評価数は確か）。全材料の安全率 6.587 は予算 1 の1回の値。同等性（`equiv.py`）は予算 3 で、予算 60 では走らせていない。予算 3 の製品の入口は、探索の後の CAD ブーリアン（除外の切り抜き）で例外になる（ほぼ全材料の形のため。探索の結果には無関係）。 (20260921-1701-topopt-search-target-factor-acceptance, 2026-09-21)
- **作業ツリーで測ると別セッションの未コミット変更を読む**（`binary_search.py` に `target_tolerance` が入った瞬間に起動した測定が読みかけて殺された）。以降は `git archive` の展開で回す。`models/hanger_bar_attachment.json` は git 管理外なので展開先へ手でコピーする。 (20260921-1701-topopt-search-target-factor-acceptance, 2026-09-21)
- **要素数が 100 万を超える細分は、殺す閾値つきで回さないと他の測定を巻き込む**。4.8 の形の k=2（約 190 万要素）は物理メモリ 17.4GB を超えた（機械は 31.7GB）。k=1（24 万要素）は 3.7GB・80 秒。細分は k ごとに別プロセスにし、空き 4GB 割れで自分で殺す。 (20260921-1701-topopt-search-target-factor-acceptance, 2026-09-21)

## 内側の目標（hanger 2.4mm・節N）

- `topopt_checks/measure_min_width_floor.py` の `--target`（探索に渡す目標。既定 2.0）と `--element-size`。結果 JSON の `target_safety_factor`・`element_size_requested_mm` には渡した値が入る。細分は `refine_adopted_mask.py --mask-npz X.npz --k 1 2 3 [4] --result X-refine.json`（合否は `--target`（既定 2.0）に対するもので、探索に渡した目標ではない）。 (20260921-0537-topopt-inner-target-hanger-curve, 2026-09-21)
- 測定は `fb7d0fb`（`duocad/` は `11a5512` = S(m) つきの製品コードと同じ）。別の版のコードの値と並べるときは版を書く。目標 2.0 の基準列は節M2 の S(m) つき探索とその採用マスクと同一の試作の細分、m=3・目標 2.0 は節M1 の試作（製品では未実施）。 (20260921-0537-topopt-inner-target-hanger-curve, 2026-09-21)
- **目標 2.0 の列は予算 60 で目標に届かず止まった形**（m=2 で 2.15、m=3 で 2.31、4.8mm で 2.59）。3.0・4.2 の形は目標に届いてから止まった（3.0007・4.2080・m=3 は 4.3136）。倍率（k=1÷k=3）を目標どうしで比べるときは削り具合が違う形どうしになる。 (20260921-0537-topopt-inner-target-hanger-curve, 2026-09-21)
- 探索は各条件1本（再走なし）で、倍率のばらつきは分からない。4.8mm の細分は **k=4 の安全率が k=3 より高い**（2形とも）ので、「k を上げると単調に下がる」を前提にしない。 (20260921-0537-topopt-inner-target-hanger-curve, 2026-09-21)
- `refine_adopted_mask.py` の結果 JSON は**全 k を解き終えてから**書く。途中で落ちると JSON が無く、行は `.stdout.log`（1 k につき1行の JSON）にだけ残る。k=3 の細分は 12 万〜19 万要素で物理メモリ約 6.5〜7.4GB、189,891 要素（m=3・目標 4.2）で 721 秒。`--budget 1` の m=2 探索も 4 分半かかる（初期の全設計空間の評価が約 165 秒）ので、「既定不変」の確認は入口で落ちる引数（`--cells 1` は最小幅ガードで落ちる）では取れない。 (20260921-0537-topopt-inner-target-hanger-curve, 2026-09-21)

## 内側の目標（合成題材 S1〜S3・節O）

- 題材（80×20×20 の設計空間・`SUBJECTS`/`SHAPE`/`FIXED_BLOCK`）: 荷重 S1=2N(-z)・S2=20N(+x)・S3=2N(-z)、全材料の安全率 148.4 / 146.2 / 148.6。他の題材・力を足すなら全材料の安全率が目標 4.2 の 25 倍（105）以上になることを確かめる。生成スクリプト `measure_inner_target_synthetic.py` は `312aac5` で削除済み（履歴から復元できる） `(対象コード不在・要確認)`。 (20260921-0538-topopt-inner-target-synthetic-shapes, 2026-09-21)
- ファイル名の規約: `<題材>-m<m>-t<目標>`（目標 2.0 は `t2`、4.2 は `t4.2`）、予算を上げた再走は `-b120`・`-b240`、細分の `--load-rule nearest` は `<タグ>-nearest-refine.json`。 (20260921-0538-topopt-inner-target-synthetic-shapes, 2026-09-21)
- 探索は `binary_search.find_best_feasible(mask, conditions, max_evaluations=N, min_width_cells=m)` をそのまま呼ぶ（差し替えなし）。表の行は「予算 60 で止まり安全率が目標の 1.1 倍未満ならその形、以上なら予算 120 の形」。予算 240 の3本は指示の外の追加で O1〜O4 の行に入れていない。 (20260921-0538-topopt-inner-target-synthetic-shapes, 2026-09-21)
- **細分の確定表は、細分後の格子で同じ物理的な面に対して作り直し、材料に接さない節点は面から外して荷重を残りの節点へ配り直す（`renormalize`）**。荷重ベクトルの合力は全 k で要求した力に一致（差 5.4e-14 N 以下）、k=1 は探索の安全率と小数 6 桁以上で一致。
  理由: 保持ソリッドの判定は「材料と交わる」ことで、保持セルは探索のマスクから削られうる。偽の確定表に面の全節点を入れたまま細分の材料に当てると、材料に接さない節点が `conditions` 検査で落ちる。 (20260921-0538-topopt-inner-target-synthetic-shapes, 2026-09-21)
- 荷重の配り直しは製品の `anchor.build_anchors(allowed_nodes)`（標本ごとの配分）と同じではない。6形での比較では S1・S3 は差が 0.01 倍以内、引張 S2 は最大 0.11 倍。全形では比較していない。 (20260921-0538-topopt-inner-target-synthetic-shapes, 2026-09-21)
- **目標に届いていない形の倍率を、削り切った形と並べて結論にしない**。m=3 で安全率が目標の 1.01 倍以内で止まったのは 6 条件中 1 本だけ（S1・目標 2.0）。S3・目標 4.2・m=3 は予算 240 でも 10.32（目標の 2.46 倍）。予算を上げると倍率は動く（S3・目標 4.2・m=2: 予算 120 の 1.57 倍 → 240 の 1.48 倍。S1・目標 4.2・m=3: 1.23 → 1.33 倍）。削り具合を揃えずに目標どうしを比べない。 (20260921-0538-topopt-inner-target-synthetic-shapes, 2026-09-21)
- 各条件1本（再走なし）。k=4 でも安全率は下がり続けた（収束していない）。設計空間は 80×20×20 の1つ・力の大きさは全材料の安全率が約 150 の1つで、他の大きさ・実部品の mm 換算への外挿は測っていない。 (20260921-0538-topopt-inner-target-synthetic-shapes, 2026-09-21)
- Python 3.11 は f 文字列の中に同じ種類の引用符を入れ子にできない（`f"{r["a"]}"` は構文エラー）。表の生成は関数に分ける。bash の heredoc 内の `python -c`/置換で `\n` を書くと実際の改行が入る。 `_python_processes()` は初期版で自分の起動用プロセスを写したので自分と親を除いた。 (20260921-0538-topopt-inner-target-synthetic-shapes, 2026-09-21)
- `docs/TOPOPT_THICKNESS.md` は 1,900 行超で CLAUDE.md の目安（1,500 行）を超えている（当時の指摘）。 (20260921-0538-topopt-inner-target-synthetic-shapes, 2026-09-21)

## warm start（撤去済み）

CG の warm start は `1d9bcea`（コード・テスト・測定スクリプトの削除）と `f7acca0`（文書）で**撤去された**。以下は撤去後の現行と、撤去前の知見のうち再利用されるものだけを残す。

- **削除直前の develop の SHA は `6998528`**（`6998528b080712abbc6e267dadfc4a64ed5e5744`）。warm start の測定スクリプト（`measure_warm_start.py`・`measure_warm_start_kcurve.py`・`measure_warm_start_residual.py`・`measure_warm_start_search.py`・`probe_warm_start_depletion.py`）は、そのコミットをチェックアウトするか `git archive 6998528 duocad topopt_checks` すれば動く。`docs/TOPOPT_SPEED.md` の「前の形の変位をCGの初期値にすると効くか」の節の冒頭にも同じコミットを書いてある。 (20260921-2240-topopt-remove-warm-start, 2026-09-21)
- **消えた面**（呼ぶと `TypeError` / `KeyError`）: `find_best_feasible(warm_start=)`・`acceptance.inspect(u0=)`・`stress.solve_displacement(u0=)`・`fem.Model.solve` / `solve_many` / `PreparedSystem.solve` の `u0=`・`FeasibleModel.displacement`・`fields['displacement']`・`fields['warm_start']`・`diagnostics['reduction']` の `warm_start` / `warm_start_used` / `warm_start_fallbacks`。
  `acceptance.inspect` の `fields` に入るのは `element_energy` だけ。評価ごとの変位が要る測定は `fem.Model.solve` の戻り値を使う。 (20260921-2240-topopt-remove-warm-start, 2026-09-21; 20260920-1809-topopt-warm-start-wiring, 2026-09-20)
- **CG は常に初期値 0 から始まる**（`spla.cg` に `x0` を渡さない）。cold の CG は削除前とビット単位で同じ（小さい題材 `fem.Model` の 30×8×8・24×10×6・20×6×6 の密度3種、`solve` と `solve_many` 2本で、反復数 202 / 486 / 338 / 696 / 311 / 639 と解のバイト列のハッシュが旧コードと一致）。`product-m2.npz`・2.4mm の cg 反復数は新旧とも 13,759。 (20260921-2240-topopt-remove-warm-start, 2026-09-21)
- **`docs/TOPOPT_SPEED.md` の表の 13,767 を期待値にしない。** 同じマスク・同じ格子を今日回すと 13,759（削除前でも同じ）。`PARALLEL_THREADS=1` でも 13,759 のままで原因は未確認。cg 反復数を別の測定と突き合わせるときは同じ日・同じ環境の値どうしで比べる。 (20260921-2240-topopt-remove-warm-start, 2026-09-21)
- 共通部品 `topopt_checks/_hanger_setup.py`（`MODELS_DIR`・`MODEL_NAME`・`PENAL`・`free_gb()`・`_setup(spacing_request[, model_name])`・`build_model`・`model_for`・`solve_once(model, density, solver) -> (displacement, stats)`）は後に `312aac5` の一度きり測定スクリプト削除で消えた `(対象コード不在)`。使われ方: `_setup` は `OptimizeOptions(target_safety_factor=2.0, search_target_factor=1.0)` 固定で探索に渡す目標は実際の目標 2.0 のまま（目標 4.8 で回すなら `dataclasses.replace(conditions, target_safety_factor=4.8)` を呼ぶ側で行う）。 (20260921-2240-topopt-remove-warm-start, 2026-09-21)
- `tests/test_binary_search_keep.py` の `warm_start` parametrize は軸だけを外し、`m` の軸と `keep ⊆ survivor` の不変条件（`u0` と無関係に全評価の入口にある）は残す。 (20260921-2240-topopt-remove-warm-start, 2026-09-21; 20260921-1641-topopt-keep-cells-never-removed, 2026-09-21)

撤去前の知見（再導入を検討するときの材料）:

- **warm start の採用形状が cold と一致しなかった**: hanger 2.4mm・予算20で `mask_hash` が一致せず（評価2の候補から分岐。cold は予算4の再走で評価0〜3が完全一致 = 再現する）。反復数の合計（139,093 対 155,948）は軌道が違うので効果の証拠にならない。予算20は段階4の大歩幅（削る量 75,415→1,359 セル）で、warm が効く1セル削りの終盤に届かない。既定オフ・導入前に本人判断、のまま撤去された。 (20260920-1809-topopt-warm-start-wiring, 2026-09-20)
- **k または親に対する割合で warm の効きを予測しない**（kcurve 測定では揃わなかった）。親の形（大・小）を探索の実際の軌道の代表とみなさない。旧 `peel` の 1セル 0.02 は内部セルの除去によるものと推定（旧コードで回し直していない）で、今の peel では 0.8〜0.9。前回の表を今の peel の結論として使わない。 (20260921-0441-topopt-warm-start-kcurve, 2026-09-21)
- 実探索の軌道での初期残差 r0（`docs/TOPOPT_SPEED.md`「実探索の軌道での初期残差 r0」）: hanger 以外・150評価より先・m>1 に当てはめない（2.4mm は終盤の抜けたセル数 1 が続く局面が 116 評価、2.0mm は中盤が 84 評価で局面の構成が格子で違う）。軌道は cold と同一（4.8mm・25評価で包んだ探索と包まない探索の採用形状の `mask_hash` が一致）。
  r0 は候補の cold solve の直前に、親 = 直前に採用された評価の解で出す。記録の要求セル数・抜けたセル数・採否は `reduction['steps']` と全件一致。副測定の解は捨てる。 (20260921-1656-topopt-warm-start-residual-trajectory, 2026-09-21)
- 測定の罠: 2つの格子を並行して走らせると秒は使えない（反復数は決定的）。別セッションの重い測定が走ると 1 反復あたり 15ms → 68ms になる。2.0mm のピーク常駐メモリは 12.9GB（2.4mm は 7.8GB）。`--head` は記録用の任意文字列で、実行したソースを検証しない。測定プロセスを `CommandLine -like '*kcurve*'` で全部止めると、コマンド行に kcurve を含む呼び出し元のシェルも巻き込まれて exit 255 になる。`multiplier`・`gap_ratio` は `_reduce_volume` の局所変数を `f_locals` で読むので、変数名が変わると null になる。 (20260921-0441-topopt-warm-start-kcurve, 2026-09-21; 20260921-1656-topopt-warm-start-residual-trajectory, 2026-09-21)
- 作業ツリーは CRLF、index は LF（autocrlf）。Python で書き換えるときは改行を保つ。バックグラウンド起動した実測は runner のターン終了で消える（切り離して起動し、前景の `Wait-Process` で待つ）。 (20260920-1809-topopt-warm-start-wiring, 2026-09-20)
- bash から Windows の python に `/c/Users/...` のパスを渡すと `sys.path.insert(0, '/c/...')` は黙って無効になる。editable install が本体チェックアウトを指すので、**旧コードの `git archive` と比べたつもりが本体どうしを比べていた**。`cygpath -m` で `C:/...` にしてから渡し、`fem.__file__` で読み込み元を確かめる。`git archive` に `models/` は入らない。 (20260921-2240-topopt-remove-warm-start, 2026-09-21)
- 削除したファイルは `git rm` でステージし、`git commit -- <削除したパス>` でパス指定コミットできる。 (20260921-2240-topopt-remove-warm-start, 2026-09-21)
