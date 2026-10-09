# 形状最適化の探索時の目標係数・内側目標の契約

この文書は、(1) 探索に渡す目標安全率 = 実際の目標 × 係数 2.4 の配線（`SEARCH_TARGET_FACTOR`）、(2) 内側の目標（探索に渡す目標を上げて細分で目標を満たすかを見る）の測定の読み方について、
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
- **結果の `safety_factor` を「実際の目標に対する安全率」と読まない**。探索の格子での値で、探索時の目標（4.8）に対する合否。実際の安全率は細分して解き直さないと分からない（倍率は `docs/TOPOPT_TARGET_FACTOR.md` §2 の表）。
  探索の診断・6条件の `checks['safety']['target_safety_factor']` も探索時の目標が入っている（`_full_material_failure_message` は「探索の格子で必要な安全率 4.8（= 目標 2 × 係数 2.4）」と言い換える）。 (20260921-1700-topopt-search-target-factor, 2026-09-21)
- **測定スクリプトの `--target` の意味は変えていない**: `measure_min_width_floor.py --target T` は探索にそのまま渡す目標（`search_target_factor=1.0` で走る）。**製品の既定（実際の目標 2.0 × 2.4）と同じ探索にするには `--target 4.8`**（`2.0 × 2.4` は浮動小数で `4.8` と等しい。2倍は正確）。
  `--element-size`（要素サイズ要求 mm。既定 2.4）も同スクリプトにある。 (20260921-1700-topopt-search-target-factor, 2026-09-21; 20260921-0537-topopt-inner-target-hanger-curve, 2026-09-21)

## 配線後の確認（§7）の読み方

- **「hanger が探索の格子で 4.8 を満たし、細分後も 2.0 を上回る」は 5000 N 模型では確かめられなかった。** 4.8 の形は 237,375 要素（設計空間の 63%）で、細分は k=1 のみ（安全率 4.8000）。k=2 は未測定。 (20260921-1701-topopt-search-target-factor-acceptance, 2026-09-21)
- **荷重の罠**: §7.1〜7.4 の測定は `models/hanger_bar_attachment.json`（git 管理外）の荷重が意図せず **5000 N** になっていた模型での値で、節N・K・J の測定は 200 N。比は 25.00（全材料の安全率 164.69 対 6.587）。
  §7.3〜7.5 の値は 5000 N の hanger のもので、係数 2.4 の確認にはなっていない。本人が 5000 N を意図して変えていたため、`hanger_bar_attachment_200n.json`（コピー）で測り直した（§7.8: m=2・目標 4.8 は 5,889 要素〈1.56%〉、m=3 は 7,869 要素〈2.09%〉）。
  **hanger の探索結果を読むときは、どの荷重の模型かを必ず確かめる。** (20260921-1701-topopt-search-target-factor-acceptance, 2026-09-21; 20260922-1515-topopt-solver-pairs-hanger-200n, 2026-09-22)
- 測定のコードは `a0a4a9c`（`git archive` の展開）で、節N（`fb7d0fb`）の列とは版が違う。**並べて差を読まない**（差の原因は切り分けていない）。 (20260921-1701-topopt-search-target-factor-acceptance, 2026-09-21)
- 探索の秒は並走の python で汚れている（反復数・評価数は確か）。全材料の安全率 6.587 は予算 1 の1回の値。予算 3 の製品の入口は、探索の後の CAD ブーリアン（除外の切り抜き）で例外になる（ほぼ全材料の形のため。探索の結果には無関係）。 (20260921-1701-topopt-search-target-factor-acceptance, 2026-09-21)
- **作業ツリーで測ると別セッションの未コミット変更を読む**（`binary_search.py` に `target_tolerance` が入った瞬間に起動した測定が読みかけて殺された）。以降は `git archive` の展開で回す。`models/hanger_bar_attachment.json` は git 管理外なので展開先へ手でコピーする。 (20260921-1701-topopt-search-target-factor-acceptance, 2026-09-21)
- **要素数が 100 万を超える細分は、殺す閾値つきで回さないと他の測定を巻き込む**。4.8 の形の k=2（約 190 万要素）は物理メモリ 17.4GB を超えた（機械は 31.7GB）。k=1（24 万要素）は 3.7GB・80 秒。細分は k ごとに別プロセスにし、空き 4GB 割れで自分で殺す。 (20260921-1701-topopt-search-target-factor-acceptance, 2026-09-21)

## 内側の目標（hanger 2.4mm・節N）

- `topopt_checks/measure_min_width_floor.py` の `--target`（探索に渡す目標。既定 2.0）と `--element-size`。結果 JSON の `target_safety_factor`・`element_size_requested_mm` には渡した値が入る。細分は `refine_adopted_mask.py --mask-npz X.npz --k 1 2 3 [4] --result X-refine.json`（合否は `--target`（既定 2.0）に対するもので、探索に渡した目標ではない）。 (20260921-0537-topopt-inner-target-hanger-curve, 2026-09-21)
- 測定は `fb7d0fb`（`duocad/` は `11a5512` = S(m) つきの製品コードと同じ）。別の版のコードの値と並べるときは版を書く。目標 2.0 の基準列は節M2 の S(m) つき探索とその採用マスクと同一の試作の細分、m=3・目標 2.0 は節M1 の試作（製品では未実施）。 (20260921-0537-topopt-inner-target-hanger-curve, 2026-09-21)
- **目標 2.0 の列は予算 60 で目標に届かず止まった形**（m=2 で 2.15、m=3 で 2.31、4.8mm で 2.59）。3.0・4.2 の形は目標に届いてから止まった（3.0007・4.2080・m=3 は 4.3136）。倍率（k=1÷k=3）を目標どうしで比べるときは削り具合が違う形どうしになる。 (20260921-0537-topopt-inner-target-hanger-curve, 2026-09-21)
- 探索は各条件1本（再走なし）で、倍率のばらつきは分からない。4.8mm の細分は **k=4 の安全率が k=3 より高い**（2形とも）ので、「k を上げると単調に下がる」を前提にしない。 (20260921-0537-topopt-inner-target-hanger-curve, 2026-09-21)
- `refine_adopted_mask.py` の結果 JSON は**全 k を解き終えてから**書く。途中で落ちると JSON が無く、行は `.stdout.log`（1 k につき1行の JSON）にだけ残る。k=3 の細分は 12 万〜19 万要素で物理メモリ約 6.5〜7.4GB、189,891 要素（m=3・目標 4.2）で 721 秒。`--budget 1` の m=2 探索も 4 分半かかる（初期の全設計空間の評価が約 165 秒）ので、「既定不変」の確認は入口で落ちる引数（`--cells 1` は最小幅ガードで落ちる）では取れない。 (20260921-0537-topopt-inner-target-hanger-curve, 2026-09-21)

## 内側の目標（合成題材 S1〜S3・節O）

- **細分の確定表は、細分後の格子で同じ物理的な面に対して作り直し、材料に接さない節点は面から外す**（`topopt_checks/refine_adopted_mask.py` は `anchor.build_anchors(space.soup, grid, allowed_nodes=acceptance.active_nodes(fine))`）。
  理由: 保持ソリッドの判定は「材料と交わる」ことで、保持セルは探索のマスクから削られうる。偽の確定表に面の全節点を入れたまま細分の材料に当てると、材料に接さない節点が `conditions` 検査で落ちる。 (20260921-0538-topopt-inner-target-synthetic-shapes, 2026-09-21)
- **目標に届いていない形の倍率を、削り切った形と並べて結論にしない**。m=3 で安全率が目標の 1.01 倍以内で止まったのは 6 条件中 1 本だけ（S1・目標 2.0）。S3・目標 4.2・m=3 は予算 240 でも 10.32（目標の 2.46 倍）。予算を上げると倍率は動く（S3・目標 4.2・m=2: 予算 120 の 1.57 倍 → 240 の 1.48 倍。S1・目標 4.2・m=3: 1.23 → 1.33 倍）。削り具合を揃えずに目標どうしを比べない。 (20260921-0538-topopt-inner-target-synthetic-shapes, 2026-09-21)
- 各条件1本（再走なし）。k=4 でも安全率は下がり続けた（収束していない）。設計空間は 80×20×20 の1つ・力の大きさは全材料の安全率が約 150 の1つで、他の大きさ・実部品の mm 換算への外挿は測っていない。 (20260921-0538-topopt-inner-target-synthetic-shapes, 2026-09-21)
- bash の heredoc 内の `python -c`/置換で バックスラッシュ+n を書くと実際の改行が入る。 (20260921-0538-topopt-inner-target-synthetic-shapes, 2026-09-21)

## CG の初期値と運用の罠

- **CG は常に初期値 0 から始まる**（`spla.cg` に `x0` を渡さない）。前の形の変位を初期値にする warm start は撤去済みで、測定結果は [../TOPOPT_SPEED.md](../TOPOPT_SPEED.md)「前の形の変位をCGの初期値にすると効くか」。 (20260921-2240-topopt-remove-warm-start, 2026-09-21)
- bash から Windows の python に `/c/Users/...` のパスを渡すと `sys.path.insert(0, '/c/...')` は黙って無効になる。editable install が本体チェックアウトを指すので、**旧コードの `git archive` と比べたつもりが本体どうしを比べていた**。`cygpath -m` で `C:/...` にしてから渡し、`fem.__file__` で読み込み元を確かめる。`git archive` に `models/` は入らない。 (20260921-2240-topopt-remove-warm-start, 2026-09-21)
- 測定プロセスを `CommandLine -like '*<名前>*'` で全部止めると、コマンド行にその名前を含む呼び出し元のシェルも巻き込まれて exit 255 になる。 (20260921-0441-topopt-warm-start-kcurve, 2026-09-21)
