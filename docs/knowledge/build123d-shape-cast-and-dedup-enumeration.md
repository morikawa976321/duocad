---
name: build123d-shape-cast-and-dedup-enumeration
description: 事実(2026-08-10実測) — 生のTopoDS_Shapeをbuild123dで包むには基底のShape.castではなくCompound.castを使う(基底は抽象でNoneを返す。Compound.castは実型へ振り分けソリッドにはSolidを返す)。faces()/edges()は内部がTopExp.MapShapes_sなので重複除去済みで、OCP直算と一致する
created: 2026-08-10
updated: 2026-08-10
---

# build123dで生の形状を包む正しい入口と、列挙が重複除去済みであること

`build123d==0.11.1` / `cadquery-ocp-novtk==7.9.3.1.1`（import名は `OCP`）で実測した。
OCPで操作し、問い合わせだけbuild123dに任せる構成（[[occt-provenance-enables-persistent-naming]]
の帰結）では、**生の `TopoDS_Shape` をbuild123dのラッパへ移す一歩**が最初に要る。

## 基底の `Shape.cast` は使えない

```
Shape.cast(shape)     -> None          # 抽象メソッド。実装が無く黙ってNoneが返る
Compound.cast(shape)  -> Compound      # コンパウンドを渡した場合
Compound.cast(box)    -> Solid         # ソリッドを渡した場合(実型へ振り分ける)
Solid.cast(compound)  -> KeyError <TopAbs_ShapeEnum.TopAbs_COMPOUND: 0>
```

`build123d.topology.Shape.cast` は `@abstractmethod` で、**例外にもならず `None` を返す**。
気づかずに使うと後段で `AttributeError` になり、原因が入口から遠い場所に出る。
**実型を問わず包みたいときの入口は `Compound.cast`** で、これは中でLUTを引いて
正しいラッパ型（Solid/Compound等）へ振り分ける。クラス名が用途と一致していないので、
「Compoundを作る呼び出し」に見えて実は汎用ディスパッチャである点が読みにくい。

## `faces()` / `edges()` は重複除去済み

STEPから読んだ形状（56面）で、build123dの列挙とOCP直算を突き合わせた:

| 数え方 | 面 | 辺 |
|---|---|---|
| build123d `len(w.faces())` / `len(w.edges())` | 56 | 66 |
| OCP `TopExp.MapShapes_s` + `TopTools_IndexedMapOfShape.Extent()` | 56 | 66 |
| OCP `TopExp_Explorer` で素朴に列挙 | — | 132 |

build123dの `_entities()` は内部で `TopExp.MapShapes_s` を使うため**重複除去済み**であり、
OCP直算と一致する。一方 `TopExp_Explorer` で素朴に回すと、**隣接する2面から同じ辺を2回拾って
ちょうど倍（66→132）になる**。辺の数を数える実装は、どちらの数え方をしているかで
黙って倍違う値を返すので、数え方を決めて書き留めておかないと後から比較できなくなる。

## 帰結

問い合わせ層をbuild123dで書き、種別判定や体積のように**OCCTの値そのものが契約になる部分**だけ
OCPへ委譲する分担が成立する。列挙のセマンティクスが一致しているので、
「面数はbuild123dで数え、面の種別はOCPで判定する」を混在させても内訳の合計が合う。

関連: [[build123d-ocp-abstraction-gap-measured]] / [[occt-provenance-enables-persistent-naming]]
