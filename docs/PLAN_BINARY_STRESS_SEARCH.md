# 二値モデル・応力制約による体積削減ループへの移行計画

作成日: 2026-09-17。状態: **段階1〜6(FEM分離・共通検査・初期合格探索・削除追加置換探索・製品配線・旧コード/文書削除)は実装済みで、対応する回帰テスト群は緑。段階7(通し検証・監査)を実施した結果、hangerでの実機検証は失敗した(§10.2)。全文pytestには本計画と無関係な既存失敗2件も別途ある(§10.4)。体積削減機能の完成とは扱わない。**
調査基準: `codex/pre-smoothing-checks` の `bd49a9b`。
前提: 設計空間導出は完了。検査実装は `TOPOPT_PRE_SMOOTHING_CHECKS.md` に記載。
本書は当初「後継ループの計画」として書いたが、§1〜§9は段階1〜6で実装が完了した現行仕様の記述に相当する(コードと一致することは`tests/test_doc_code_agreement.py`他で確認済み)。§10に段階7の検証結果を追記した。

## 1. 目的・範囲・固定する判断

目的は「6条件を満たす二値モデルのうち、探索で発見した最小体積のものを返す」こと。
大域的最小性、任意の入力での解の存在・発見、平滑化後の強度は保証しない。
速度より、合格モデル以外を成功として返さないこと、失敗の原因が追跡できることを優先する。

- セルは材料あり／なしのbool。体積は材料セル数×セル体積。
- 候補生成は領域単位の削除・追加・置換。採否は実際の応力と6条件で決める。
- **初版ではコンプライアンスを候補順位にも使用しない**。補助指標としての再導入は別の判断とする。
- SIMP・OC・MMA・連続密度・材料量の二分／Illinois探索は削除する。旧方式への切替・フォールバックは残さない。
- 固定済みの設計空間、格子、アンカー、荷重、材料物性は実行中に変更しない。
- 安全率は現行のvon Mises応力99.5パーセンタイルと許容応力から計算する。最大応力も併記する。
  最大応力基準への変更や印刷材料モデルの変更は本計画で混ぜない。
- 最小部材幅の新たな保証は導入しない。密度フィルタ半径を別の意味の「幅」として流用しない。
- メッシュ化・平滑化・最終ブーリアンのアルゴリズム改良は範囲外。
  ただし採用マスクを既存の後段へ渡す配線と、旧SIMP依存の除去は範囲内。

## 2. 合格モデルの契約

検査対象は候補に対して島除去を終えた、平滑化前の材料マスク。以下を全て通す。

1. 境界が閉じ、角・辺だけの接触による非多様体境界がない。
2. 6近傍で単一の材料成分。空集合は不合格。
3. 各保持CADとの共通体積が数値許容差を超える。
4. 各除外CADに材料で埋まっていない正の体積がある。
5. 元の各荷重・拘束面のアンカー節点が材料に接し、荷重の欠落・再配分がない。
6. そのマスクでFEMを再構築し、残差検算を通過した安全率が目標以上。

設計空間外に材料を追加しない。全保持体積の固定や保持マスクの塗り直しは行わず、(3)の契約で判断する。
設計空間自体が既に除外セルを除去していることは維持する。(4)だけを理由に設計空間を拡張しない。
幾何検査で失敗した候補はFEMを省略してよい。省略と計算失敗を安全率不足へ変換しない。
形状を修正したら既存の合格判定を無効にし、修正後のマスクで検査し直す。

## 3. 単一の製品経路

```text
条件文書
 → 固定設計空間を1回導出
 → 二値の初期候補を生成
 → 候補ごとの島処理・幾何条件検査・FEM応力評価
 → 初めての6条件合格を保存
 → 削除・追加・置換を試し、探索候補と最良合格モデルを更新
 → 最良合格モデルをキャッシュなしで再検査
 → boolマスク・格子・検査結果・探索終了理由を確定保存
 → 製品では、その後だけ既存のメッシュ化・平滑化・ブーリアンへ渡す
```

候補ごとのメッシュ生成・STL出力は廃止する。探索の入口はCADの追加も行わない。
後段へ渡す必要がある場合だけboolを0/1配列へ変換し、表示上の閾値0.5をアダプター内で固定する。
この変換は連続密度探索の復活ではない。

## 4. 探索状態と候補生成

### 4.1 状態

- `best_feasible`: 発見した最小体積の6条件合格モデル。上書きは小体積の合格候補だけ。
  同体積なら安全率が高い方、同値ならマスクハッシュで決定的に選ぶ。
- `frontier`: 次の試行の出発点。初期合格の探索中は幾何合格・安全率不足の候補も持つ。
  合格発見後も、体積が少し大きい別形状や安全率の余裕がある形を限定数保持する。
- `visited`: マスクと評価条件の識別子、評価結果。重複マスクを無限に試さない。
- `history`: 全試行の親、操作、除去前後の体積、6条件、FEM成否、採否理由。

未合格候補をfrontierに入れることと、返却可能なモデルにすることを分離する。
返却は必ず `best_feasible` から行う。安全率不足・計算失敗で最良モデルを失わない。
評価条件の識別子は格子・固定空間・荷重拘束・物性・応力規則・検査許容差を含める。
形状ハッシュだけで、違う入力の合格判定を再利用しない。

### 4.2 最初の合格モデル

最初は設計空間の全材料マスクから始める。条件に触れない成分を除去し、6条件を検査する。
最大成分だけを残して条件を失う候補を、成功として通さない。

境界不良なら、その周辺の許可セルの追加・削除を小さい変更から生成する。
保持・条件面の接続を失えば、元の候補へ戻し、異なる操作を試す。
全材料でも応力合格は保証されないため、応力不足なら同じ削除・置換操作で別形状を探索する。
「満杯の候補が不合格だから全て不可能」と結論しない。

修復処理を無限に入れ子にしない。修復も通常の候補生成・評価・履歴・予算の対象にする。
初期候補を発見できなければ `no_feasible_found`。入力矛盾の確定と、探索で未発見を区別する。

### 4.3 操作と順序

初版の領域形状は格子上の軸整列ブロックに固定する。球・侵食・SIMP等の選択式にしない。
大きさはセル単位の1、2、4…の決定的な系列で、設計空間の寸法を上限にする。
ブロック中心・境界での切り詰め・並び順も固定して、同じ入力と予算で再現可能にする。

1. **削除**: 材料の表面に接するブロック、および内部空間を作るブロックを候補にする。
   大きな削除が失敗したら、小さな領域も試す。低応力だけを理由に削除を確定しない。
2. **追加**: 設計空間内の、既存材料と面で接する空セル群を追加する。
   支えを作り替える探索の足場として、体積増加もfrontier内では許す。
3. **置換**: 追加と削除を1つの候補として評価する。途中の片側操作だけで不合格になっても、
   置換後に合格する変更を試せるようにする。

候補操作は削除優先としつつ、各巡回に追加・置換の枠を確保する。削除候補だけで予算を使い切らない。
frontierの保有数・追加の連続回数・試行予算を明示的な設定にする。
数値は小型題材で決めて1組を既定として固定し、採用理由と試行数を記録する。
frontierの順位は幾何合格、応力評価成功、目標達成、体積、安全率の余裕を使い、
形状の違う同体積候補を残す。局所的な行き詰まりを「最小」と呼ばない。

**段階4実装時の追記(2026-09-17、`duocad/topopt/binary_search.py` の `find_best_feasible` /
`_reduce_volume`)**: frontierは§4.1の「幾何合格・安全率不足の候補も持つ」を、評価前の生マスクを
`deque(maxlen=frontier_capacity)` に積む形で実装した(段階3のfrontierと同じ生マスク方式。容量を
超えたら最も先に積まれた候補から捨てる、完全に決定的な規則)。「追加の連続回数」は、あるマスクへ
至るまでの**連続する**'add'操作の回数を候補ごとに持たせ、それが上限を超える'add'の子だけ生成を
やめる(`delete`・`replace`はこの回数を毎回0へ戻す)。3つの値は次の小型題材で決めた:

- 題材: 格子(5,2,1)(設計空間10セル)。両端列(x=0とx=4、各2セル)が固定面・荷重面の全断面被覆に
  必須、中間(x=1..3)は幅1セルの連結で足りるよう `target_safety_factor` を無意味な小ささにした
  (安全率で形状が決まらないようにし、幾何と体積だけを見る題材にした)。
  初期合格モデルとして、中間経路が遠回り(4セル、直線なら2セルで足りる)の8セル形状を与えた。
  この形状は**どの削除でも不合格になる**(両端の各セルは全断面被覆に必須、中間の4セルは
  固定面側と荷重面側をつなぐ唯一の経路上の関節点)。削除だけを辿る探索は初期形状の部分集合
  256通り(2^8)を尽くしても8.0セルのまま(実測)。真の最小は7セル(全設計空間10セルの
  全列挙2^10=1024通りで確認)で、初期形状に無いセルを新たに使う直線経路でしか届かない。
- **frontierの保有数=48**: この題材で32以下だと、全設計空間から始めた通しの探索
  (段階3の初期合格→段階4の削減)が7.0へ届かず8.0のまま行き詰まる(容量32・予算1500での実測:
  `neighbourhood_exhausted`、885試行、8.0セル)。48以上(48・64・96・128で確認)は同じ題材で
  安定して7.0へ到達する(容量48・予算2000での実測: `neighbourhood_exhausted`、932試行)。
  詰まった手製の初期形状から段階4の削減だけを走らせた場合は32でも7.0へ届く(898試行)が、
  全設計空間からの通し探索は候補の分岐が大きく、32では有望な追加系の候補がfrontierから
  溢れて捨てられる。48を既定にして両方の経路で余裕を持たせた。
- **追加の連続回数=2**: 同じ題材(手製の初期形状から段階4のみ)で0(追加を一切使わない)だと
  182試行で行き詰まり8.0のまま(削除だけと同じ結論で、追加を使わない場合の再現として妥当)。
  1以上で7.0へ届く(1は742試行、2は898試行、3は983試行、5は932試行——3以上は単調に改善する
  わけではなく、大きくしても速くはならない)。1でも足りるが、実運用の題材では1回の追加だけで
  支えを作り替えられない場合もあるため、2を既定にして余裕を持たせた。
- **試行予算(`max_evaluations`の既定)=1000**: 段階3・段階4を通した1本の予算(§4.1)。
  上記題材で全設計空間からの通し探索が7.0へ収束するまでに実測932試行かかったため、
  この題材規模なら既定1000で収束を確認できる。より大きい題材では既定のままでは
  収束せず`evaluation_limit`で終わる(想定どおり——大域最小の保証はしない、§1)。
- **既知の簡略化(未実装のまま残した部分)**: 上記104〜105行が求める「frontierの順位は
  幾何合格、応力評価成功、目標達成、体積、安全率の余裕を使う」は実装していない。
  段階3と同じ、評価前の生マスクをそのまま`deque`に積むFIFO方式のままにした。
  体積の昇順のような安価な事前指標だけで優先度を付ける設計を上記題材で試したところ、
  'add'系の候補(体積が一時的に増える)が常に'delete'系より低優先度になり、frontier容量を
  増やしても改善が一切見つからない(8.0のまま)という**FIFOより悪い結果**になった
  ('delete優先'と'追加・置換の枠を確保する'の両立には、事前指標に基づく優先度と
  容量確保だけでは足りず、少なくとも二重プールの枠取りと明示的な巡回制御が要ることを
  実測で確認したが、今回の範囲では設計しきれなかった)。FIFO+容量+連続追加回数の制限
  という現行案は、この題材では既定値で大域最小に届くことを確認済みだが、frontierの
  順位付けそのものは段階4の完了条件(§8「削除だけで詰まる題材で、追加/置換による改善が
  実際に起きることを確認」)を満たす範囲でしか検証していない、既知のギャップとして残す。

### 4.4 島除去

最大成分だけを選ぶ既存処理を探索の正として使わない。
各連結成分を残した場合の条件成立を調べ、全荷重・拘束と各保持との重なりを満たせる成分を候補にする。
複数候補があれば別々に評価する。成立する成分がなければ候補は不合格。
除去した島のセル数・体積・含まれていた条件を記録する。
最後に保持を塗り直して島を復活させたり、空洞を埋めて黙って別形状にしたりしない。

### 4.5 応力評価

材料セルだけで剛性を組み、空セルの微小剛性で橋渡ししない。
材料に接しない節点だけを自由度から外し、浮いた材料への人工拘束は追加しない。
元の荷重をNで保存・適用し、相反する荷重の相殺で脱落を隠さない。
既存の変位ソルバー、残差検算、von Mises・単位換算を再利用する。
解法変更による再試行は有限の規則にし、同じマスク・条件であることを記録する。
計算失敗は `unknown` であり、応力不足や体積の下限の証拠にはしない。

### 4.6 終了と中断

終了理由は少なくとも `neighbourhood_exhausted`、`evaluation_limit`、`cancelled`、
`no_feasible_found`、`evaluation_error`。形状の合否とは別の欄にする。
「指定した近傍で改善なし」を大域収束・最小性証明と表示しない。
合格候補ありで予算終了なら、最良候補を最終再検査して返せる。
中断時は合格チェックポイントを記録するが、製品文書へ勝手に結果を追加しない。
最終再検査が失敗したら成功を返さず、候補と診断を残す。

## 5. モジュール構成と既存実装の移行

| 配置案 | 責務 |
|---|---|
| `duocad/topopt/fem.py` | `simp.py` から再利用する二値FEM・線形解法・残差検算を抽出 |
| `duocad/topopt/material.py` | 許容応力・ヤング率・印刷低減係数の唯一の定義 |
| `duocad/topopt/acceptance.py` | 境界・単一成分・条件・安全率の共通検査、合否の型 |
| `duocad/topopt/binary_search.py` | 候補生成・frontier・最良候補・停止・履歴。関心が分かれれば候補生成だけ別モジュール化 |
| `duocad/topopt/stress.py` | 材料マスクと変位から応力・安全率。二値FEMへの依存に変更 |
| `duocad/optimize_run.py` | 条件文書、固定空間、新探索、最終検査、既存後段の接続 |
| `topopt_checks/check_pre_smoothing.py` | 保存マスク・過去実行を読むCLI。製品用の検査ロジックを複製しない |

既存の `pre_smoothing_acceptance.py`、`pre_smoothing_geometry.py` は製品からimportしない。
共通検査を本体へ移し、CLIはその呼び出しだけにする。CAD共通体積演算はkernel側の適切なモジュールへ移す。
`tests/test_boundaries.py` のOCPアクセス境界を守り、検査を削除・全面免除して通さない。
現検査の剛体運動・荷重欠落・残差・ボクセル境界のテストを移植し、意味を保持する。
過去記録の密度→二値変換は読み取りアダプター内に限定し、新規の製品APIはboolマスクを受ける。

## 6. 不要コードの削除一覧

削除は本体・引数・例外・既定値・呼び出し元・専用テストまで一体で行う。
別フォルダへの退避、非推奨ラッパー、旧新切替フラグで残さない。履歴はgitを使う。

| 対象 | 作業 |
|---|---|
| `simp.py` | FEM・線形ソルバーを抽出後、ファイル自体を削除。連続密度の `optimize`、OC/MMA更新、ペナルティ継続、密度フィルタ、体積目標、反復結果、プリセット実行を削除 |
| `simp.Model` 周辺 | 要素剛性・節点番号・行列組立・CG/direct/MG・残差検算を残す。`rho**penal`、`emin` による空セル剛性、密度由来の動的active set・そのキャッシュ／閾値を削除。boolの静的active maskは残す |
| 反力制約 | `ReactionGroup`、反力下限制約・感度・MMA配線を削除。荷重釣り合い確認に使う反力計算だけ再利用する。拘束面そのものや荷重は削除しない |
| `safety_loop.py` | 材料定数の移動後、ファイルを削除。`solve_for_safety_factor`、Illinois／二分提案、材料量上下限、`too_little/too_much` 分類、旧結果型を除去 |
| `design_space.py` | 固定空間導出・接続検査は維持。`run_topopt` とSIMP/STLへ直結する実行CLIを削除または新しい明示的CLIへ移す。導出アルゴリズムは変更しない |
| `anchor.py` | `AnchorTable`・面積配分・荷重組立を残す。`run_pipeline` 内のSIMP・表面生成・保持密度塗り直しと、それだけの引数を削除。条件構築を副作用のない関数へ分離 |
| `stress.py` | `simp` import・密度閾値による母集団選択・ペナルティ引数を削除。母集団は材料マスク。99.5%規則、実単位、最大応力・変位を維持 |
| `optimize_run.py` | `_material_bounds`、`MATERIAL_MIN_RATIO/MAX_RATIO`、SIMP反復進捗、材料量補間履歴、旧速度推計式を削除。単一の新探索APIに接続 |
| `mesh.py` | SIMPプリセットを回すCLIと探索候補ごとの呼び出しを削除。平滑化等の本体は維持。採用マスクに対して再島除去・保持塗り直しが起きない明示的入口を作る |
| `voxelize.py`、`__init__.py` | SIMP固有の便宜API・説明を点検し、不要分を削除。軸順、格子、CAD占有、マスクモデルは維持 |
| `bench.py` | 本体から検査設定への依存をなくす。必要なFEM・幾何fixture設定は検査側へ移し、SIMP・OC/MMA・フィルタ専用設定と不要プリセットを削除。最終的に本ファイルを削除 |
| `trace.py` | 新しい候補・二値マスク・評価結果・終了理由の記録に置換。FEM記録から密度専用属性を除去。損失なし配列保存・例外・出典記録は維持 |

FEMを含む大きなファイルを機械的に丸ごと削除してから書き直さない。
移設した計算の等価性を確認してから旧定義を消し、同じ計算の二重実装を残さない。

### 6.1 検査・計測・生成物

- 削除: `check_filter_radius.py`、`measure_mma_vs_oc.py`、`measure_reaction_constraint.py`、
  `measure_simp_convergence.py`、`plot_simp_safety_convergence.py`、`plot_simp_volume_convergence.py`、
  `profile_simp_iter_breakdown.py`。新方式に名前だけ変えて旧比較機能を残さない。
- `check_simp.py` はFEM検査だけ `check_fem.py` 等へ移して削除。
- `check_safety_loop.py` は新ループの独立検査へ置換して旧ファイルを削除。
- `check_absolute_volume.py` は二値体積・単位換算の必要な検査を移し、連続密度の体積制約検査と旧ファイルを削除。
- `measure_active_set.py` は密度に基づく動的active比較を削除。静的な材料要素だけのFEM組立検査はFEM検査に統合。
- `measure_safety_search.py` は二分対Illinois比較を削除。必要な候補評価数・FEM回数・最良体積履歴の計測だけ新ループ用に作り直す。
- `check_multigrid.py`、`check_parallel.py`、`check_stress.py` は二値の既知形状で検証し、SIMPで題材を作る経路を削除。
- `check_anchor.py`、`check_design_space.py`、`check_mesh.py`、`check_voxelize.py` は各層の検査を保持し、
  SIMP終端まで走る部分や連続密度体積との一致を要求する部分を削除・置換。
- `check_optimize_wiring.py`、`check_trace.py` と `tests/test_optimize_backend.py`、`test_optimize_run_ui.py`、
  `test_optimization_trace.py`、`test_pre_smoothing.py`、`test_doc_code_agreement.py` を新APIへ移す。
  中断・元文書不変・CAD保存・記録再現性の検査は失わない。
- `duocad/topopt/output/` の `simp_*`、`mma_vs_oc.json`、`reaction_constraint.json`、`active_set*.json`、
  旧 `safety_search.json`、参照／case5専用のSTLなど、廃止コード専用の追跡済み生成物を削除する。
  残す検査のSTLは生成先を整理し、不要なものは削除。担当外の未コミット生成物は触らない。
- 保存済み実行を読む `analyze_recorded_stages.py`、`render_recorded_analysis.py` 等は維持可能。
  新形式と旧形式をschemaで区別し、旧方式を再実行する依存は除去する。
  過去ログのイベント名・密度値を読むこと自体は、旧最適化ロジックを残す理由にしない。

### 6.2 製品API・UI

- `OptimizeOptions` から `rmin_mm`、`max_iter`、`material_min_mm3/material_max_mm3` を削除。
  `max_evaluations` は新しい候補評価予算として契約を定義し直す。内部の線形反復上限と混同しない。
- `session.py`、`catalog.py`、`net/ws.py`、`web/src/session.ts` の引数・型・送受信を同時更新。
  旧引数が外部から届いたら黙って無視せず、不支持と分かるエラーにする。
- `OptimizePanel.svelte`、`App.svelte`、`commands/optimize.ts` から密度フィルタ半径と旧進捗を削除。
  目標安全率、要素サイズ、最良合格体積、評価回数、終了理由を表示する。
- 解像度の上限から `rmin / MIN_RMIN` を除去。条件面・要素数上限・実格子への丸めは維持して再検証。
- 所要時間は旧SIMP係数を使わない。初回は不明と表示し、実測FEM評価時間と予算から幅付きで見積もる。
- `optimizeDebug.ts` の密度表示を二値材料表示へ変更し、設定・イベント名の不要分を削除。
  条件の注釈表示や設計空間表示は維持する。
- `smoke-optimize.mjs`、`smoke-optimize-debug.mjs`、`shoot-topopt.mjs`、`shoot_topopt_export.py` を新入力へ更新。
  rmin比較しか目的がない撮影機能は削除する。

## 7. docs/ の整理

文書を「現行の新仕様」と「再現しない過去の測定」に分け、旧仕様を現在形で残さない。
廃止文書はアーカイブフォルダへ移さず削除する。価値のある実測・制約だけ関連する現行文書へ短く移す。

| 文書 | 作業 |
|---|---|
| `TOPOPT.md` | 現在の全経路を新方式で書き直す。SIMP設定、材料量二分探索、密度閾値、単調性を前提にした成功説明を除去 |
| `TOPOPT_MMA_VS_OC.md` | 比較対象が消えるので削除 |
| `TOPOPT_REACTION.md` | 反力下限・MMA制約の文書を削除。荷重釣り合いや拘束の注意だけFEM説明へ移す |
| `TOPOPT_SIMP_ITER_BREAKDOWN.md` | 削除。線形解法の有用な実測だけ条件付きの過去値として速度文書へ移す |
| `TOPOPT_ACTIVE_SET.md` | 動的密度閾値比較は削除。静的材料要素の組立・未使用節点処理をFEM説明へ統合し、本ファイルを削除 |
| `TOPOPT_SPEED.md` | 旧係数・旧実行コマンドを削除。候補数・FEM回数・幾何検査時間・キャッシュ・最終再検査の実測へ更新 |
| `TOPOPT_RECORDING.md` | 候補マスク、親と操作、島除去、6条件、合格チェックポイント、終了理由、旧記録の読み取り範囲を明記 |
| `TOPOPT_PRE_SMOOTHING_CHECKS.md` | 製品共通検査への移設、bool API、再評価規則、新CLIを記載。旧NPZ／連続密度は過去記録入力として区別 |
| `TOPOPT_ACCEPTANCE.md` | 平滑化前6条件と平滑化後の水密・単一ソリッドを分ける。前者合格から後者合格・強度を推論しない |
| `PLAN_FIXED_DESIGN_SPACE.md` | 完了済みの記録として維持。後段SIMP配線等は本計画で置換された旨を冒頭に記す。固定空間の決定理由は保持 |
| `DESIGN_DECISIONS.md`、`UI.md` | 旧最適化方式・設定UIの現行説明を置換 |
| `DEV_NOTES.md`、`QA_LESSONS.md`、`SMOKE_TESTING.md` | 廃止CLI・テスト・ファイルへの参照を修正。一般的な開発知見は残す |
| `INITIAL_PLAN.md` | 当初計画であることを明記し、現行手順と混同させない。新仕様の重複記載先にしない |
| `README.md`、`docs/README.md`、`tests/README.md`、`topopt/__init__.py` | 入口・索引・依存関係・テスト一覧を更新し、削除文書のリンクを解消 |

本表以外も `docs/` 全体を検索する。旧語が出ることだけで無関係な説明を消さず、
実行手順／現行仕様／明示した歴史記録のどれかを判定する。
**本計画は段階7でこの表を実行済みとして検証した(§10)。** 未実装案を現仕様として残さない
という方針どおり、§1〜§9はコード・テストと一致する現行仕様の記述であり、
「採用方針の計画」ではなく「実装済みの記録」として読むこと。

## 8. 実装順序と段階ごとの完了条件

1. **FEM分離**: 二値の棒・梁・空セル領域で現行FEMとの変位・応力・反力の一致を確認。
   既存経路の単位・軸順・荷重を壊していないことを固定する。
2. **共通検査とbool契約**: 検査を製品側へ移設し、幾何・CAD・境界条件・FEMの役割を分離。
   キャッシュの有無で結果が一致し、エラーはunknownとなることを確認。
3. **初期合格探索**: 小型題材で合格モデルの保存まで完成。未発見・矛盾・解像度不足を区別。
4. **削除・追加・置換探索**: 最良モデルの保持、決定性、予算、重複回避、中断を実装。
   削除だけで詰まる題材で、追加／置換による改善が実際に起きることを確認。
5. **製品配線**: 候補中はメッシュ化を呼ばず、最終確定マスクだけ後段へ渡す。UI/API/ログも更新。
6. **削除と文書整理**: §6・§7を全て実行。新旧併存を移行完了としない。
7. **通し検証・監査**: 小型製品試験、hangerの平滑化前6条件、削除漏れ、リンク切れを検証して完了。
   → **実施したが、hangerでの実機検証は失敗した(§10.2)。体積削減機能の完成と扱わない。**

最終マージは独立したコミット単位でFEM移設、共通検査、探索、製品、削除・文書をレビューできる構成にする。
作業開始時に最新mainとこのワークツリーの差を確認し、別作業のリベースや未コミット変更を上書きしない。

## 9. 検証と完成条件

- 各6条件を単独で破る反例を用意し、合格に化けないことを検証する。
- 複数保持・複数荷重・相反荷重・複数拘束、格子未満のCAD、境界接触、内部空洞を含む。
- 孤立成分が条件を持つ例、全材料でも応力不足の例、計算失敗、初期合格未発見を検証する。
- 安全率再評価が削除・島除去後のマスクを使うことを、マスクハッシュと解への入力で確認する。
- 最良モデルは合格を維持し体積が増えない。探索用候補の体積増加と混同しない。
- 合格発見後の全試行失敗、予算終了、中断、最終再検査失敗で返却状態が正しい。
- 記録から候補と採否を再現でき、失敗候補を含めFEM回数・時間・体積履歴が追える。
- 微小格子では許可マスクの全列挙を検査専用の正解として使い、評価・最良候補選択を照合する。
  通常探索が常に大域最小を発見するというassertにはしない。
- hangerは保存済みの同一入力、同一物性・目標・要素サイズを基準にする。
  新ループが返す採用マスクで6条件全合格、最終再検査一致、導出1回、候補中メッシュ化0回を必須とする。
  初期合格だけで止まって軽量化できない場合は、その事実と終了理由を記載し、体積削減機能の完成と扱わない。
- 速度の合否閾値は設けず、実時間・候補数・FEM回数・削減体積を報告する。
- 既存後段の小型製品保存・undo/redo・元ボディ不変・中断を確認する。
  hangerの後段に残る平滑化問題を、新ループの合格や未解決事項から隠さない。

最後に全実行コード・テスト・UI・docsを検索し、`simp`、`safety_loop`、`run_preset`、
`_OCUpdate`、`_MMAUpdate`、`penal_at`、`volfrac`、`rmin_mm`、`MIN_RMIN`、`reaction_min`、
`dynamic_active`、`material_min_mm3`、`material_max_mm3`、`illinois`、旧 `compliance` 進捗を監査する。
残るものは過去記録の読み取り・明示された歴史説明・本計画の削除一覧に限定し、理由を一覧にする。
FEM内部の反復法や他機能の二分探索など、同じ単語を持つ必要な処理は削除しない。

成果物は新しい単一経路、再利用FEM、共通6条件検査、更新UI/記録/CLI、整理した文書、
削除一覧、回帰結果、hangerの採用マスクと検査報告。旧方式を残した暫定状態では完了にしない。
**「hangerの採用マスクと検査報告」は§10.2のとおり未達**(初期合格にすら到達していない)。

## 10. 段階7: 通し検証・監査の結果(2026-09-17)

### 10.1 §9完成条件のうち小型題材で確認済みのもの

pytest全体は1206 passed / 2 skipped / **2 failed(後述10.4、本計画と無関係な既存不具合)**。
以下は§9の各項目に対応する既存テスト(段階1〜4で作成済み。本タスクは新規作成せず存在を確認した):

| §9の項目 | 確認したテスト |
|---|---|
| 6条件を単独で破る反例 | `tests/test_pre_smoothing.py`(`test_regular_boundaries[cavity\|tunnel]`、`test_corner_and_edge_contacts_fail`、`test_local_singularity_is_detected_even_if_globally_connected`、`test_cancelled_loads_are_checked_per_face`、`test_force_scale_and_underconstrained_solid`、`test_solver_failure_is_unknown_not_unsafe` 等) |
| 内部空洞・境界接触・格子未満のCAD | 同上 `test_regular_boundaries[cavity]`(内部空洞)・`test_corner_and_edge_contacts_fail`(境界接触) |
| 孤立成分が条件を持つ例 | `tests/test_binary_search.py::test_resolve_islands_rejects_an_isolated_component_that_holds_only_one_condition` / `test_resolve_islands_keeps_the_component_that_alone_satisfies_every_condition` |
| 全材料でも応力不足 | `tests/test_binary_search.py::test_full_material_still_fails_safety_is_not_laundered_into_a_pass` |
| 計算失敗・初期合格未発見 | `test_computation_failure_is_recorded_as_unknown_and_never_becomes_the_best_feasible` / `test_no_feasible_found_records_history_and_does_not_return_a_dangling_best` / `test_resolution_insufficient_is_reported_without_running_the_search` / `test_input_contradiction_is_confirmed_when_even_the_full_design_space_cannot_preserve` |
| 安全率再評価が削除・島除去後マスクを使う(マスクハッシュ一致) | `test_island_is_stripped_before_the_safety_recheck_mask_hash_and_solver_input_agree` |
| 最良モデルは体積が増えない | `test_reduction_is_deterministic_for_the_same_input_and_budget`、`_is_smaller_feasible` の構造(体積→安全率→マスクハッシュの順で決定的に選ぶ) |
| 予算終了・中断・最終再検査失敗での返却状態 | `test_evaluation_budget_exhaustion_keeps_the_best_feasible_found_so_far` / `test_cancellation_returns_the_best_feasible_found_before_the_cancel_signal` / `test_cancellation_after_an_improvement_preserves_the_improved_checkpoint` / `test_failed_final_recheck_keeps_the_previous_best_and_does_not_crash` |
| 削除だけで詰まる題材での追加/置換の実効果 | `test_pure_deletion_cannot_improve_on_the_detour_shape` / `test_add_and_replace_escape_the_deletion_deadlock_the_pure_deletion_test_hits` |
| 微小格子の全列挙照合 | `test_tiny_grid_exhaustive_enumeration_matches_the_search_optimum` / `test_default_budget_and_frontier_settings_reach_the_optimum_on_this_tiny_fixture` |
| 既存後段の小型製品保存・undo/redo・元ボディ不変 | `tests/test_finalization.py::test_solid_operation_undo_redo_and_replay`(`add_solid_body`経路は全verb共通の履歴機構) |
| 中断(製品配線) | `tests/test_optimize_backend.py::test_cancelling_leaves_the_document_untouched` / `test_search_cancellation_leaves_the_document_untouched` / `test_search_cancellation_discards_a_found_best_feasible_too` |
| 候補評価中メッシュ化0回 | 構造的に保証(`duocad/topopt/binary_search.py`・`acceptance.py`は`mesh`モジュールを一切importしない)。回帰は`tests/test_optimize_backend.py::test_no_candidate_meshing_and_the_confirmed_mask_reaches_finalize` |

これらは段階1〜4がそれぞれの完了条件として自ら作成・QA済みのテストであり、本タスクは
新規作成ではなく**存在と合格を確認した**(監査)。複数保持・複数荷重・相反荷重・複数拘束の
組み合わせ網羅までは追えていない——`tests/test_optimize_backend.py::_setup_four_conditions`が
2保持体・単一拘束面・単一荷重面の組み合わせを、`test_pre_smoothing.py`の各fixtureが
単体条件の反例を提供しているが、「複数荷重」「相反荷重」「複数拘束面」を同時に持つ
単一のテストは見つからなかった。**本人判断待ち**: この網羅性の不足を段階7の不合格理由に
含めるかどうか(下記10.2の失敗が優先度としてより重大なため、本タスクでは追加のテストを
書いていない)。

### 10.2 hangerでの実機検証: 失敗(体積削減機能の完成と扱わない)

**結論: 現状のコードはhangerに対して初期合格すら発見できず、探索が例外(`MemoryError`)で
異常終了する。§9が求める「6条件全合格・最終再検査一致・導出1回・候補中メッシュ化0回」の
いずれも確認できていない。**

手順: `models/hanger_bar_attachment.json`(このワークツリーの`models/`は`.gitignore`対象のため
`C:\Users\tm976\mf\duocad\models\`から複製して使用)、`target_safety_factor=2.0`
(docs/TOPOPT_PRE_SMOOTHING_CHECKS.mdのCLI例・2026-09-09完走時の実測と同じ値)、
`topopt_checks/measure_binary_search.py`(段階6が新設・未実機検証のままだったもの)を
要素サイズ4.8mm(2026-09-09実測表で「通る」と確認済みの解像度。2.4mmは規模が大きすぎて
本タスクの時間内に実行できないため不採用。§9は要素サイズの固定値を指定していない)・
`max_evaluations=500`(製品既定1000より小さいが、実時間を踏まえた明示的な予算。§9に
既定予算を使う指定はない)で実行した。

**症状**: `find_initial_feasible`の探索ループ内、`_generate_repairs`→`repair_candidates`が
候補マスク(`shape=(36, 66, 52)`、1配列あたり約121KiB)を生成する箇所で
`numpy._core._exceptions._ArrayMemoryError`が発生し、プロセスが異常終了した
(起動から約8〜9分後。実行時のログはtaskboxのworkspace配下に一時保存したが、taskboxの運用規則により
タスク完了時に削除される使い捨て領域のため、恒久的な記録はこの節の記述と`git log`のコミット
メッセージ(`6439e16`)が正)。

**根本原因の切り分け(`acceptance.inspect`を1回だけ直接呼ぶ診断で特定)**:
hangerの設計空間の**全材料マスク**(削除前・最大体積)で6条件のうち「荷重・拘束が有効」
(`checks['conditions']`、面`load_face_31`)が**要素サイズ4.8mm・2.4mmを含む全テスト解像度
(4.8/4.4/4.0/3.6/3.2/2.8/2.4mm)で恒常的に不合格**だった。`load_face_31`のアンカー総節点数は
要素サイズが細かくなるほど当然増える(4.8mmで170節点→2.4mmで938節点)。そのうち
どの材料セルにも接していない節点(`lost_nodes`)は49〜108件(**総節点数に対する割合は
10.4%〜31.5%とばらつき、単調な傾向は無い**。実測: 4.8mm 28.8% / 4.4mm 31.5% / 4.0mm 20.1% /
3.6mm 18.4% / 3.2mm 10.4% / 2.8mm 15.9% / 2.4mm 11.5%)。2.4mmでは`fixed_face_10`/
`fixed_face_7`も新たに2節点ずつ失い始め、悪化する。`margin_ratio`を要素サイズ4.8mm固定のまま
既定0.20から0.35/0.50/0.80まで振ると`lost_nodes`は49/36/50/40件(総節点数も168〜176の間で
微動する——`margin_ratio`は格子の外枠自体も広げるため、アンカー総節点数に全く影響しない
わけではないが、`lost_nodes`の水準は解消しない)——**単純な余白不足ではない**。
`design_space.derive`は凸包由来の候補マスクを`connect_conditions`で拘束面・荷重面・
保持シードへの接続性で剪定する(`duocad/topopt/design_space.py`の`derive`内、731行目)。
マージンを広げても`lost_nodes`が解消しない挙動は、この接続性剪定がhangerの荷重面(トーラス)
近傍の材料を独立成分として落としている可能性を示唆するが、**この先の特定(`connect_conditions`の
どのステップが該当セルを落としているか)は本タスクの範囲外**(`design_space.derive`自体は
2026-09-16の別作業で導入されたもので、本計画の段階1〜6のどれも変更していない)。

**この2つの事実から言えること**:

1. **全材料マスクが既に「荷重・拘束が有効」条件を落とすなら、削除ベースの修復
   (`repair_candidates`は既存材料の削除・追加・置換しか行わない)では原理的に解決できない**
   ——材料を減らしても荷重面のアンカー被覆は改善しない。にもかかわらず
   `find_initial_feasible`はこれを「幾何不良」や「応力不足」と同列に扱い、
   `_generate_repairs`で新しい候補を生成し続ける(§4.2「幾何不良でも応力不足でも同じ
   `repair_candidates`を使う」の設計どおりだが、この失敗モードには効かない)。
2. **`find_initial_feasible`のfrontier(`deque`)には`_reduce_volume`の
   `frontier_capacity`(既定48)に相当する上限が無い。** 解決不能な失敗が繰り返されるたびに
   `_generate_repairs`が生成する候補が際限なく積み上がり、`max_evaluations`(pop数の予算)に
   到達する前にメモリを使い尽くす。小型テスト題材(格子10セル程度)では候補数が少なく
   顕在化しないため、段階3・段階4のテストはどれもこの経路を踏んでいない
   (段階4の後続契約③が「実運用の格子規模での収束を検証していない」と明記していた懸念が
   実際に的中した)。

**本人判断待ち(次の一手)**:

- (a) `design_space.derive`の`connect_conditions`がhangerの荷重面近傍で材料を落としている
  原因を特定し、設計空間の側で被覆を直す(担当は本計画の外——`design_space.py`は
  2026-09-16の別作業の産物で、本計画は「導出アルゴリズムは変更しない」と宣言している
  ため、ここを触るのは新しい判断が要る)。
- (b) `find_initial_feasible`に、`preserve_on_full`の入力矛盾判定(352〜363行目)と対になる
  「拘束面・荷重面のアンカーが全材料マスクでも被覆されない」検出を追加し、
  `resolution_insufficient`/`input_contradiction`と同様に**探索を試みず即座に確定した
  失敗として返す**(削除では解決できないと分かっている以上、探索する意味が無い)。
- (c) `find_initial_feasible`のfrontierに`_reduce_volume`と同様の容量上限を設け、
  たとえ根本原因(a)を直さなくても**クラッシュではなく`no_feasible_found`で終わる**ようにする
  (堅牢性の最低限)。
- 上記いずれも本タスク(段階7・通し検証)の担当ではない実装変更のため、本タスクでは
  適用していない。次のタスクが着手する場合は上の3案から選ぶか、優先順位を付けること。

**§9が求める報告(速度の合否閾値なし)**: 初期合格に到達しなかったため、候補数・FEM回数・
削減体積は測定できていない。実時間は約8〜9分(クラッシュまで)。使用した要素サイズは4.8mm
(design_space.derive後の材料マスク47,232要素、格子箱123,552要素)、目標安全率2.0。

**§9の「hangerの後段に残る平滑化問題を隠さない」について**: 探索が初期合格にすら
到達しないため、平滑化以降の後段(メッシュ化・平滑化・最終ブーリアン)はhangerに対して
一度も実行できていない。stage5で発見された既知の別問題(既定解像度が
`voxelize.occupancy`のレイキャスト頑健性バグを踏む、docs/TOPOPT.md「制限」節に記載)とは
別の、より手前の段階でのブロッキング要因である。

### 10.3 §9末尾の監査語の一覧(コード・テスト・UI・docs全体)

全件grepはこのタスクの実行時にtaskboxのworkspace(使い捨て領域。完了時に削除される)へ
一時保存した。要約(語ごとの残存理由。再現するには本節末尾の監査語をこの文書全体で
`grep -rniIE`すればよい):

| 監査語 | 残存箇所の種別 | 判定 |
|---|---|---|
| `simp` | `docs/TOPOPT.md`の日付見出し付き歴史節(2026-08-31〜2026-09-16の実測記録)、`docs/PLAN_BINARY_STRESS_SEARCH.md`/`PLAN_FIXED_DESIGN_SPACE.md`の削除一覧(§5・§6表自体)、`docs/QA_LESSONS.md`の過去のインシデント記録、`tests/test_design_space.py`ほかの「SIMPは回さない/追っていた検査の後継」という否定形の言及、`topopt_checks/analyze_recorded_stages.py`・`render_recorded_analysis.py`の**旧記録読み取り**(過去のtrace JSONに`"name": "simp"`のフェーズが実在するため必要)、`topopt_checks/check_fem.py`の移設前基準値コメント | 許容(過去記録の読み取り・明示された歴史記録・本計画の削除一覧) |
| `safety_loop` | 上記simpと同様の歴史節・削除一覧・`tests/test_doc_code_agreement.py`のコメント例示 | 許容(同上) |
| `run_preset` | `PLAN_BINARY_STRESS_SEARCH.md`自身の監査語列挙(この段落)のみ | 許容(自己言及) |
| `_OCUpdate` / `_MMAUpdate` / `penal_at` | 監査語列挙のみ、または`docs/TOPOPT.md`の歴史節(`simp.penal_at`)1件 | 許容 |
| `volfrac` | すべて`docs/TOPOPT.md`の日付付き歴史節(2026-09-04実測等)。コード内に`volfrac`引数は現存しない(grep 0件) | 許容(歴史記録) |
| `rmin_mm` / `material_min_mm3` / `material_max_mm3` | `duocad/session.py`・`net/ws.py`・`catalog.py`・テストに**意図的に残存**——段階5の契約どおり、旧引数名を受け取って`OptimizeInputError`で拒否するための識別子。削除すると「旧引数が届いたら分かるエラーになる」契約(§6.2)を満たせない | 許容(削除禁止・意図的残存) |
| `MIN_RMIN` | `docs/TOPOPT.md`の歴史節のみ。コードに定数は無い(grep 0件) | 許容(歴史記録) |
| `reaction_min` | `docs/QA_LESSONS.md`・`TOPOPT.md`の歴史節のみ | 許容(歴史記録) |
| `dynamic_active` | `docs/TOPOPT.md`の歴史節(段階6直前の削除決定を含む)のみ。コードに機構は無い(段階6で削除確認済み、`20260917-0445`契約) | 許容(歴史記録) |
| `illinois` | `docs/TOPOPT.md`の歴史節1件のみ | 許容(歴史記録) |
| 旧`compliance`進捗 | `web/src/session.ts`に`compliance`は0件(段階5で削除確認)。`duocad/topopt/fem.py`の`compliance`メソッドと`check_fem.py`/`check_multigrid.py`の同名テストは**FEM検査で使う現役の計算**であり、監査対象の「SIMP反復ごとの進捗表示」とは別物 | 許容(別概念) |

**本タスクで修正した2件**(監査中に発見した、意図的でない現在時制の誤り):

- `topopt_checks/check_design_space.py`(553行目): 検査ラベル文字列「拘束面から固定自由度が
  引ける(**SIMPに渡せる**)」→「(**fem.pyに渡せる**)」。`simp.py`は段階6で削除済みのため、
  このラベルは存在しないモジュールを指していた(検査結果の合否には影響しない、表示文字列のみ)。
- `topopt_checks/check_parallel.py`(220〜223行目): `fem.PARALLEL_THREADS`の代入について
  「`fem.PARALLEL_THREADS`は`fem`からの再輸出」という自己言及的で意味の通らない記述と、
  存在しない`simp`側の束縛への言及が残っていた。`simp.py`が段階6で削除され
  `PARALLEL_THREADS`は`fem.py`だけが持つ唯一の実体になったため、コメントを現状に合わせて
  書き直した(段階6以前は`simp.PARALLEL_THREADS`という別束縛があり、そちらを書き換えても
  `fem.PARALLEL_THREADS`に反映されない罠が実在したが、`simp.py`削除後はその罠自体が消えている)。

### 10.4 pytest全体の結果と、本計画と無関係な既存失敗2件

`pytest -q`: 1206 passed, 2 skipped, **2 failed**。失敗2件は本計画のどのコミットより前
(`0613907`、2026-09-16、本計画の開始コミット`2223908`より前)から存在し、
`git stash`でも同じ2件が失敗することを確認した(本タスクの変更は`topopt_checks/`の
コメント2箇所のみで、いずれもpytest収集対象外):

- `tests/test_boundaries.py::test_only_kernel_module_imports_ocp` — `kernel_brep.py`、
  `mesh_geometry.py`、`topopt/finalize.py`がOCPを直接importしており、境界検査の
  免除一覧(`OCP_ALLOWED`)に未登録。
- `tests/test_mcp_verbs.py::test_tool_names_match_the_verb_names_in_the_operation_log` —
  `add_solid_body`というverbがoplogに登録されているが、MCPツール名の期待集合
  (`LOGGING_TOOLS`)に含まれていない。

いずれも「二値モデル・応力制約による体積削減ループ」とは無関係な、別の作業
(`0613907`「preserve diagnostics, finalize CAD constraints, and document fixed-space plan」)
由来の既存不具合であり、本計画の削除・実装作業は関与していない。**本タスクでは修正していない**
(担当外)。**本人判断待ち**: 別タスクとして起票するか、次にこの領域を触るタスクの担当に含めるか。
