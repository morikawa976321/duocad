# 形状最適化の CG を GPU で解く

形状探索の FEM の線形求解(ヤコビ前処理つき CG)を、NVIDIA の GPU で解く経路。**既定は CPU** で、使う機械だけ人が有効にする。
決めた経緯と実測は [gpu_feasibility/CONCLUSION.md](../gpu_feasibility/CONCLUSION.md)(morikawa976321/duocad#104)。

- 実寸 2.0mm の探索 1 本で 2.3〜2.6 倍(デスクトップ、RTX 5060 Ti。同 CONCLUSION.md 表 3)。
- **解・反復数は CPU の `scipy.sparse.linalg.cg` とビット一致する。** 探索の試行と採用形状も変わらない。途中で CPU に戻っても結果は同じ。
- 直接解法に回る小さい系(`solver="auto"` の `direct`)は対象外。

## 有効にする

画面の「設定」(ヘッダーの「設定」ボタン)の3つ目、**「GPU で計算する」にチェックを入れる**。次に準備する計算から効き、
走っている最適化の途中で切り替えても答えは変わらない(秒だけが変わる)。

- 選んだ値はこの clone の設定ファイル `duocad_settings.json`(リポジトリ直下・未追跡。`duocad/local_settings.py`)に残り、
  サーバを立て直しても続く。安定版と開発版は別の clone なので別の値になる。
- **GPU の部品(CuPy と CUDA ランタイムの wheel、約 2GB)が入っていなければ**、設定の下に「GPU の部品を入れる(約 2GB・再起動)」
  ボタンが出る(安定版だけ)。押すとサーバが `tools/stable/update.ps1 -InstallGpu` を起こして落ち、`uv sync --frozen --extra gpu`
  だけを走らせて立ち直る(取り込みとビルドはしない。main に新しいコミットが無くても使える)。失敗したら extra なしで同期し直して
  CPU の構成に戻す。開発版では手で `uv sync --extra gpu` を走らせる。
- 以後の更新(`update.ps1`)は、その版が extra gpu を持ち、かつ設定が gpu か CuPy が入っているときに `--extra gpu` を付ける。
  **extra を付けない `uv sync` は入っている CuPy を消す**ので、GPU を切っても部品は入れたままにする(切り直すたびに入れ直さない)。
- 設定の下の1行が今の状態を言う: CPU で解いている/GPU の名前/部品が入っていない/使えない理由(CPU で解いている)。
- コードから使うなら `fem.FEM_DEVICE = "gpu"`、1 回だけなら `Model.prepare(..., device="gpu")`。

GPU の無い機械(ThinkPad P14s Gen 6 等)では選ばない。選んでも、最初の solve で CPU に戻ってそのプロセスでは以後試さない。

## CPU に戻る条件と記録

`gpu` のとき、次のどれかで **CPU が初期値から解き直す**(途中の解は渡さない。渡すとビット一致が崩れる)。

| 理由(`gpu_fallback` の頭) | 中身 | 以後 GPU を試すか |
|---|---|---|
| `cupy:` | CuPy を読めない・GPU が無い | 試さない |
| `openblas-threads:` | numpy 同梱 OpenBLAS のスレッド数を読めない | 試さない |
| `startup-check:` | 起動時に小さい系を CPU と GPU の CG で解き、ビットが合わない(scipy の行列積・更新の番人) | 試さない |
| `dot-mismatch:` | その長さの内積が `np.dot` とビット一致しない(numpy・OpenBLAS の番人。長さとスレッド数ごとに 1 回検査) | 他の長さは試す |
| `vram:` | VRAM が足りない | 試す |
| `residual:` | 収束したのに残差検算に通らない | 試す |
| `gpu-error:` | それ以外の CUDA の例外 | 試さない |

- 戻るたびに `RuntimeWarning` を出す。
- 実行記録の `linear-solve` に `device_requested` / `device`(実際に解いた方)/ `gpu_fallback`(理由。戻らなければ null)が残る。
  `fem.collect_solve_stats()` は `gpu_solves` / `gpu_fallbacks` を数える。**`gpu_fallbacks` が 0 でない実行は、CPU と GPU の秒が混ざっている**
  (`wall_seconds` で手法どうしを比べる集計では分ける)。実行記録の `environment` には `fem_device`(その時点の設定)が残る。
- 解き終わるたびに VRAM をドライバへ返す(Unity・ローカル LLM と 16GB を分け合うため)。

## ビット一致の範囲

保証するのは「**同じ機械・同じ numpy/scipy で、GPU の ON/OFF が結果を変えない**」ことだけ。

- 内積の足し順は OpenBLAS のスレッド数(既定は論理プロセッサ数)と実行時カーネルで決まる。**機械をまたぐ一致は CPU 同士でも元々無い。**
- numpy・scipy を上げると自己検査に落ちて CPU に戻ることがある(結果は変わらず、速度だけ戻る)。版は `uv.lock` で固定してあり、
  上げるときは GPU のある機械で `tests/test_fem_gpu.py` を回す。
- 確認した組み合わせ: numpy 2.4.6(OpenBLAS 0.3.31)・scipy 1.17.1・cupy-cuda13x 14.2.0、Python 3.11 と 3.14、Core Ultra 5 245KF。

## テスト

`tests/test_fem_gpu.py`。CPU へ戻る経路は GPU が無くても走る。ビット一致と探索の一致は、CuPy と GPU がある環境でだけ走る
(開発用チェックアウトで回すなら `uv sync --extra gpu`)。
