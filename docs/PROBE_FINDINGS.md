# 検証結果 — ocp_vscode を CAD deixis レイヤーの土台にできるか（duocad の前史）

実施日: 2026-08-07 / 実施者: Claude (Opus 5) / 環境: Windows 11, Python 3.11.15
バージョン: **build123d 0.11.1 / ocp-vscode 4.0.1**（engineノートの調査時は v4.0.0）

> **【2026-08-13 保存の経緯】この文書は `mf/cad-deixis-probe/` にあった検証フォルダの成果物である。**
> 同フォルダは使い捨て前提で作られ、**役目を終えたので検証スクリプトと `.venv`（603MB）ごと削除した**。
> 残したのはこの結果だけ（手順書 `README.md` は削除したスクリプトの実行方法しか書いておらず、
> 残すと誤解を招くため捨てた）。
>
> **なぜ残すか**: [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md) §7 の落とし穴
> （フィレットの非単調な失敗・添字の破壊・STEP往復）の**一次記録がここにある**。
> engineにも蒸留済みだが、数値の細部はこちらが出典。
>
> **なぜ duocad の前史なのか**: この検証が「**ocp_vscode の上には交互操作が載らない**」を確定させ、
> 自作（duocad）へ進む根拠になった。特に【発見3】の「添字が面/エッジの種別を失う」が、
> 自作すべき差分が想定より1段大きいという結論を生んでいる。
> **なお ocp_vscode はもう使っていない**ので、本文中の「土台として使える」等の評価は**当時のもの**として読むこと。

## 何を検証したのか

engineノート `operational-notes/ocp-vscode-selection-channel-discovery`（2026-07-20）は
**ソース読解のみ・実機未検証**のまま置かれていた。その中心的な主張はこれ:

> 公開API `ocp_vscode.listener(callback)` を使い、自作スクリプトから
> `AnalysisTool.SELECT` アクティブ時の `selectedShapeIDs` イベントを直接
> subscribe すれば、コード改変なしで「クリック→トポロジ添字文字列」の配信が動く。
> 20〜30行程度。

この主張の真偽を実機で確かめた。

## 結論（3行）

- **「クリック→添字→Pythonの形状オブジェクト」の経路は実在し、実際に動いた。** 方針の土台としては使える。
- **ただしノートの手順そのものは動かない。** キー名（`selectedShapeIDs`）とAPI（`listener()`）が
  どちらも実態と違う。動く手順は本ドキュメントに書き換えてある。
- **一番効くのは新しく見つかった欠陥のほう**: 送られてくる添字は
  **面・エッジ・頂点の区別を失っている**。deixisレイヤーの設計はここに影響を受ける。

---

## 検証1: 添字空間は一致するか → **PASS**

`python check_index_alignment.py`（GUI不要・再現可能）

- ビューア側の添字を作る `get_faces` と、Python側 `select_faces` が使う `get_faces` は
  **同一の関数オブジェクト**だった（どちらも `ocp_tessellate.ocp_utils.get_faces`）。
- WebSocket転送と同じ **serialize → base64 → deserialize の往復を挟んでも、面の列挙順序は完全に保存された**（7面すべて一致）。ここが唯一の実質的な争点だったが、通った。
- `select_face(part, i)` は全7面で i 番目の面を返した。

→ **クリックで得た添字をそのまま `select_face` に渡してよい。** ノートのこの部分は正しい。

## 検証2: クリックはPythonに届くか → **届く。ただし経路はノートの記述と別物**

### 【発見1】`selectedShapeIDs` は SELECT モードでは飛んでこない

ビューア（three-cad-viewer）のソースを実機で追うと、SELECT ツールの通知はこれだけ:

```js
notify() {
    const indices = this.selectedShapes.map((s) => this._getIndex(s.name));
    this.viewer.checkChanges({ selected: indices }, true);   // ← selectedShapeIDs ではない
}
_getIndex(name) { return name.split("_")[1]; }               // "faces_6" → "6"
```

WebSocket の実データを傍受して確認した。SELECT 有効時に流れるのは:

```
U:{"command":"status","text":{ ... , "activeTool":"SelectObjects", "selected":["1","2","6","6"]}}
```

`selectedShapeIDs` は**キーごと存在しない**。あれは Distance/Properties ツール専用だった。
ノートが `backend.py` で読んだ `selectedShapeIDs` は、SELECT とは別系統のツールの通り道である。

### 【発見2】`listener()` は standalone ビューアでは使えない（しかも表示を壊す）

`standalone.py` の WebSocket ルーティング:

```python
elif message_type == "L":
    self.javascript_client = ws            # ← L: は「ブラウザが登録した」の意味
    print("Info: Browser as viewer client registered")
```

`ocp_vscode.listener()` は接続時に `L:Python listener` を送る。つまり standalone では

- Python listener に更新が**中継されない**（`U:` はサーバ内で消費されて終わり）
- それどころか **`javascript_client` の枠をブラウザから奪う**ので、以後モデルの表示が壊れる

実際、listener を走らせた状態でクリックしても Python 側には**何も届かなかった**。
`listener()` が想定しているのは **VS Code拡張版**のビューアで、standalone とは中継の実装が違う。

### 【発見3】添字は種別（faces/edges/vertices）を失っている ← 設計に効く

`_getIndex("faces_6")` も `_getIndex("edges_6")` も **`"6"`** を返す。
実測でも、面と エッジを1つずつ選んだ結果が `selected: [..., "6", "6"]` になり、
**どちらが面でどちらがエッジか復元できなかった**。

deixisレイヤーは「ユーザーが指したもの」を正確に受け取ることが唯一の仕事なので、
**この欠落は自作差分の中で最初に埋めるべき箇所**になる。埋め方は3つ:

1. ビューアの `selectObject.notify()` に相当する所へパッチを当て `name` をそのまま送る
2. 種別を「面」に固定して運用する（面だけ指せればよいなら実害は小さい）
3. 選択直後の `lastObject.info.path`（`/Group/Solid/faces/faces_6`）を別経路で拾う

### 【発見4】動く経路 — 公開API `status()` で取れる

`standalone.py` は `U:` を受けるたび `self.status[key] = value` に畳み込んでおり、
`C:"status"` コマンドに対してその全体を返す。これは **公開API `ocp_vscode.status()`** から叩ける。

**実測（end-to-end、成功）**:

| 段階 | 実測値 |
|---|---|
| ビューアでのクリック（JS側の判定） | `/Group/Solid/faces/faces_2`、world point z=10.0 |
| `ocp_vscode.status()` の戻り | `{"selected": ["2"], "activeTool": "SelectObjects"}` |
| `select_face(part, 2)` の解決結果 | `Face(GeomType.PLANE)` center=(-1.10, 0.55, **10.0**) normal=(0,0,1) area=2198.94 |
| 突合 | クリック点の z と解決した面の z が一致 = **同じ面を指せている** |

- 添字 → build123dオブジェクトの解決: **0.1〜0.6 ms**（無視できる）
- `status()` の往復: **中央値 38.6 ms**（min 17 / max 56、20回計測）。
  毎回WebSocketを張り直す実装なのでこの程度。0.4秒間隔のポーリングなら実用範囲。

### 【発見5】standalone では選択がクリップボードにも入る

```python
if key == "selected":
    pyperclip.copy((",").join(changes.get("selected", [])))
```

クリック後にクリップボードを読むと `2` が入っていた（実測）。
つまり **`select_faces(obj, [ここに貼る])` という運用は、今日この瞬間、自作コード0行で成立している。**
「まず既製の組合せで摩擦を実測する」段階の**最小の出発点はこれ**。

## 検証3: 生の添字は壊れるか → **壊れる。実例を取得した**

`check_index_alignment.py` の [5]。箱の右側面（+X向き・x=30）を追跡し、形状を変えて添字を見る:

| 変更 | 追跡面の添字 | 判定 |
|---|---|---|
| 基準形状 | i=5 | — |
| 既存の穴を 8→5 に縮小 | i=5 | 保持 |
| 穴をもう1つ追加 | i=5 | 保持 |
| ポケットを穴より前に追加 | i=5 | 保持 |
| 上面の外周に面取り | i=5 | 保持 |
| **縦エッジにフィレット** | **i=9** | **★ずれた** |

同じ面を指しているのに添字が 5→9 に動いた。
**「生の添字を保存してはいけない／意味的セレクタ式への逆合成が要る」という前提は、
推測ではなく実測で裏付けられた。** 同時に、多くの変更では動かないことも分かった —
つまり**壊れ方が散発的**で、素朴に使うと「たまに間違った面を指す」という最悪の壊れ方をする。

---

## ノートの記述の訂正（engineへ反映すべき差分）

| ノートの記述 | 実測 |
|---|---|
| `AnalysisTool.SELECT` は config に定義があるだけでハンドラ未実装 | **ビューア側には実装がある。`show(analysis_tool=...)` で実際に有効化でき、動く** |
| SELECT時に `selectedShapeIDs` を subscribe すればよい | **誤り。SELECT が送るキーは `selected`** |
| `listener(callback)` で 20〜30行 | **standalone では機能しない（表示も壊す）。standalone は `status()` ポーリングが正** |
| ID形式は `{obj_id}/faces/faces_{i}` | ビューア内部（`info.path`）ではその形式。**Pythonまで届く時点では裸の添字に落ちている** |
| 添字は `select_faces` と同一空間 | **正しい（実測でPASS）** |
| 添字はトポロジカルネーミング問題で壊れやすい | **正しい（フィレットで再現）** |

## 未検証のまま残ること

1. **VS Code拡張版での `listener()` の挙動。** 拡張は**この環境に未インストール**（`code --list-extensions` に無し）。
   ノートの手順が前提にしていたのは拡張版なので、`listener()` が拡張版では中継されるのは確からしいが、
   **その場合でも受け取れるキーは `selected`**（送信側は共通のビューアライブラリなので）。
   → 手順書 `README.md` のステップ4で確認できるようにしてある。
2. **実マウスによるクリック。** ブラウザペインが非表示だと `requestAnimationFrame` が止まり
   GPUピッキングが走らないため、レンダリングとピック確定をJSから手動で駆動した。
   **GPUピッキング自体は正常に動作した**（クリック座標→面の特定が正しく行われた）ので、
   経路の検証としては成立しているが、「実際にマウスで押したときの操作感」は測れていない。
   → **これは本人にしか測れない。手順書の目的はそこにある。**
3. **`build123d-mcp` は今回入れていない。** 昇格ゲートが要求する「既製の組合せでの摩擦の実測」の
   もう半分（LLM側の形状検査ループ）は手つかず。

## この検証が昇格ゲートに対して意味すること

`digital-improvement-stance` の昇格ゲートは「まず既製の組合せで摩擦を実測してから差分だけ作る」。

- **既製の部分は思ったより出来ている**: 表示・ホットリロード・GPUピッキング・選択の取得までは
  自作コードなしで通る。土台として使えるという判断は維持してよい。
- **自作すべき差分は、ノートの想定より1段大きい**: 「添字→セレクタ式の逆合成」だけでなく、
  その手前に**「種別を失った添字を、指した対象の同定にまで戻す」層**が要る（発見3）。
- **摩擦の実測はまだ終わっていない。** 動くことは分かったが、**本人が実際に手で使ったときの
  速さ**（Fusion360の直接操作と比べて何倍遅いのか）は測れていない。次に必要なのはコードではなく、
  本人が `README.md` の手順を1回通すこと。
