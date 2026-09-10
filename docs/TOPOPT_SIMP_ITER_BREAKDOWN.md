# SIMP 1反復の時間内訳（hanger 2.4mm）

**この文書の役割:** `simp.optimize` の**設計反復1回**に何秒掛かり、塊を小さくしても下がらない理由と、
秒数の主な寄与先をまとめたもの。**実測の正**は `duocad/topopt/output/simp_convergence_hanger_2.4mm.json`
と `topopt_checks/profile_simp_iter_breakdown.py` の出力。

| 何が | どこにあるか |
|---|---|
| 外側ループ（安全率探索）の短縮案 | [TOPOPT_SPEED.md](TOPOPT_SPEED.md) |
| マルチグリッド・CPU並列の設計判断 | [TOPOPT.md](TOPOPT.md)「幾何マルチグリッド前処理」「CPU並列化」 |
| 材料量スイープの生データ | `duocad/topopt/output/simp_convergence_hanger_2.4mm.json` |
| 反復内訳の計測スクリプト | `topopt_checks/profile_simp_iter_breakdown.py` |
| **本書** | 1反復の内訳・床の理由・短縮の当たり所 |

---

## 現象

`models/hanger_bar_attachment.json`・要素サイズ **2.4mm**・連続値保持（既定導出）で SIMP を回すと、
**材料量を下げて塊を小さくしても、1反復あたりの秒数は大きくは下がらない**。

本番経路（`optimize_run.run()` → `safety_loop` → `run_topopt`）は `threads` を渡さないので、
`simp.PARALLEL_THREADS`（この機械では **7** = 論理コア14の半分）で動く。
その条件下では **おおよそ 30〜35秒/反復** が床と観測される。

`topopt_checks/measure_simp_convergence.py` は並列測定の公平性のため **`threads=1`** 固定で回す。
同じ題材・同じ格子でも **1反復はおおよそ 100〜130秒** になり、本番の 3〜4倍遅い
（並列は matvec だけ効き、RAP/`splu` は直列のまま —— [TOPOPT.md](TOPOPT.md)「CPU並列化」）。

---

## 実測（2026-09-09〜10）

### 環境

- Windows 11 / Python 3.11.15 / duocad `.venv`
- 題材: `hanger_bar_attachment.json`
- 格子: **78×138×109 = 1,173,276 箱要素**（案1の外接直方体。材料量では変わらない）
- 解法: `solver="mg"`（既定）、`cg_rtol=1e-9`

### 材料量スイープ（`measure_simp_convergence.py`・threads=1・80反復）

| ケース | 材料比率 | 塊要素 | 平均秒/反復 | 最小秒/反復（history） |
|---|---|---|---|---|
| 1（最小） | 6.6% | 144,790 | **125.5** | **97.8** |
| R（参照） | 8.0% | 175,503 | 131.5 | 104.1 |
| 2 | 10.5% | 230,348 | 138.6 | 103.2 |
| 3 | 19.0% | 416,820 | 170.4 | 104.0 |
| 4 | 33.0% | 723,950 | 217.0 | 133.0 |
| 5（最大） | 55.0% | 1,096,894 | 335.9 | 202.4 |

参照 run（容量 8%・10反復のみ）: **5047秒 → 505秒/反復**（塊 175,503 要素・threads=1）。

**読み方:**

- 塊を **144k → 1.1M 要素**（約7.6倍）に広げると、平均秒/反復は **125 → 336秒**（約2.7倍）。
  線形では伸びないが、**小さい塊でも 100秒/反復（threads=1）** は確実に掛かる。
- 最小ケースの **最小反復 97.8秒** を本番の 7 スレッドで粗く割ると **約 28〜35秒**
  （matvec 並列の効きは完全線形ではないが、観測の「34秒床」と整合する）。

生データ: `duocad/topopt/output/simp_convergence_hanger_2.4mm.json`

### 反復内の段別計測（`profile_simp_iter_breakdown.py`・threads=7・2反復）

案1の**全塊**（active **1,096,894** 要素）で `collect_solve_stats` とフィルタ計測をした
部分プロファイル（最小塊ではなく、solve 偏重がはっきり出る条件）。

| 段 | it=1 | it=2 | 備考 |
|---|---|---|---|
| **合計** | 373.3秒 | 286.7秒 | `simp.optimize` の `Iteration.seconds` と同型 |
| `model.solve` | 342.5秒 | 255.1秒 | **91〜89%** |
| 体積制約 OC ループ | 28.3秒 | 29.1秒 | 内側で `_physical()` → 全格子フィルタを繰り返す |
| 密度フィルタ（当該反復の合計） | 26.1秒 | 26.9秒 | OC + 最終 `_physical` 等の `_filter_conv` 呼び出し |
| `dcn`/`dvn` 用フィルタ | 0.83秒 | 0.87秒 | 全格子 × 2 |
| `compliance` | 1.18秒 | 1.20秒 | 小さい |

**`solve` 1回あたりの内訳**（2反復合計 ÷ 2 solves）:

| 項目 | 秒/solve | 比率（solve内） |
|---|---|---|
| `assemble`（K 組立） | 48.1 | 16% |
| `slice`（自由 DOF 切出） | 3.2 | 1% |
| **`mg_setup`（RAP + 最粗 LU）** | **24.3** | **8%** |
| **CG / V-cycle** | **242.0** | **75%** |
| CG 反復数 | 161〜306（平均 234） | 密度が二値に寄ると増える |

フィルタ単体（同格子・1回・threads 不問）: **0.51秒/回**（3回平均）。
OC の二分探索は `_physical()` を何十回も呼ぶため、**反復あたりのフィルタ合計は数秒〜数十秒**
になりうる（上表の 26秒は全塊・7スレッド条件）。

---

## 1反復で何をしているか

```
simp.optimize() の for it in 1..max_iter:
  ├─ model.solve(x_phys)          … K組立 + mg_setup + CG/V-cycle  （最大）
  ├─ model.compliance()           … 小さい
  ├─ 感度 dc, dv
  ├─ _filter_conv ×2 (dcn, dvn)   … 全格子 (nelx×nely×nelz)
  └─ OC 体積制約の二分探索
       └─ 各ステップ: _physical(xnew) → _filter_conv … 全格子
  └─ x_phys = _physical(x)        … また全格子
```

コード: `duocad/topopt/simp.py` の `optimize`（設計反復ループ）、`Model.solve`、`_filter_conv`。

---

## 塊を小さくしても秒数が下がらない理由

### 1. 格子サイズは材料量と無関係

連続値保持では、探索中のすべての評価が **同じ `reference.base.grid`**（案1の外接直方体）を使う。
`void_mask` で塊の外を空要素にするだけで、**配列の形状は常に 78×138×109**。

密度フィルタ `_filter_conv` は **常に全箱要素**に畳み込む。塊が 144k 要素でも 1.17M 箱の
コストは変わらない。

### 2. 線形方程式の規模もほぼ一定

節点自由度は **約 3,624,000**（hanger 2.4mm 実測。材料量を変えても同オーダー）。
空要素にしか接していない節点は `extra_fixed_dofs` で拘束に回すが、
**自由 DOF の桁は縮まない**。

塊が小さいと `active_mask` で **K の非零の数**は減るが、CG/V-cycle は格子全体の
前処理構造を使うため、秒数は **下がるが線形には下がらない**（実測: 塊7.6倍で秒2.7倍）。

### 3. 毎反復 MG の組み直し

マルチグリッドは **密度が変わるたびに** 各段の RAP と最粗格子の LU を組み直す
（`simp._mg_setup`）。これはスレッド並列化できない（[TOPOPT.md](TOPOPT.md) 実測）。
全塊プロファイルでは solve の **約8%** が `mg_setup` 単体。

### 4. 体積制約 OC がフィルタを繰り返す

OC 更新の二分探索（`while (l2-l1)/(l1+l2) > 1e-9`）の**各ステップ**で
`_physical()` → 全格子フィルタ。塊の大きさに依存せず、**反復あたりの呼び出し回数**が効く。

---

## 秒数の見積もりとの関係

`optimize_run.SECONDS_PER_ELEMENT_ITERATION = 1.8e-4` は
「秒 / (格子要素数 × 設計反復)」の桁取り用係数（[TOPOPT.md](TOPOPT.md)「予想時間の係数の作り方」）。

最小塊 144,790 要素 × 1.8e-4 ≈ **26秒/反復**（threads=1 の solve 寄与の見積もり）。
実測平均 125秒には OC・フィルタ多回・MG 組み直し・CG 反復数が上乗せされる。
本番 7 スレッドでは matvec 部分が短縮され、**床 30〜35秒** 付近に落ち着く。

---

## 短縮の当たり所（1反復の中身に対して）

[TOPOPT_SPEED.md](TOPOPT_SPEED.md) の外側ループ短縮と併せて、**1反復を軽くする**候補:

| 優先 | 案 | 効き | 難度 |
|---|---|---|---|
| 1 | **格子を粗くする**（2.4mm → 4.4mm 等） | 箱要素数が桁で減る。SIMP は解像度に強依存 | 画面・契約 |
| 2 | **MG setup の再利用**（密度変化が小さい反復では RAP/LU をスキップ） | solve の数〜十数% | 中 |
| 3 | **OC 二分探索のフィルタ回数削減**（初期値・打ち切り・フィルタ結果の再利用） | 数秒〜十数秒/反復 | 中 |
| 4 | **`threads` の調整** | 半コア付近が最良（[TOPOPT.md](TOPOPT.md)）。RAP は効かない | 低 |
| 5 | `cg_rtol` 緩和 | 全体 ~16% 程度。精度トレードオフ | 低（未着手） |

**効きが小さいもの:** mesh/STL（評価の 0.01%）、`build_anchors`（0.5%）——
[TOPOPT_SPEED.md](TOPOPT_SPEED.md) の内訳表どおり。

---

## 再現手順

```powershell
# 材料量スイープ（threads=1・各ケース80反復。数時間〜）
.\.venv\Scripts\python.exe topopt_checks\measure_simp_convergence.py

# 反復内訳（参照 run 構築あり。threads=1 なら参照だけ約80分）
.\.venv\Scripts\python.exe topopt_checks\profile_simp_iter_breakdown.py --threads 1

# 案1全塊・2反復だけの煙テスト（threads 既定=7）
.\.venv\Scripts\python.exe topopt_checks\profile_simp_iter_breakdown.py --smoke
```

---

## 関連コード索引

| ファイル | 内容 |
|---|---|
| `duocad/topopt/simp.py` | `optimize` 反復ループ、`Model.solve`、`collect_solve_stats`、`_filter_conv`、`_mg_setup` |
| `duocad/topopt/design_space.py` | `derive_from_reference`（格子共有）、`run_topopt` |
| `duocad/topopt/anchor.py` | `void`/`solid` マスクの組み立て → `simp.optimize` |
| `duocad/optimize_run.py` | `SECONDS_PER_ELEMENT_ITERATION`、メモリ実測コメント（2.4mm 塊 172,310 要素） |
| `topopt_checks/measure_simp_convergence.py` | hanger 材料量スイープ |
| `topopt_checks/profile_simp_iter_breakdown.py` | 段別秒数の計測 |
