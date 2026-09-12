---
name: dataclass-standalone-module-load
description: 運用知見 — importlibでモジュールを単体ロードして中の@dataclassを使うなら、exec_moduleより前にsys.modulesへ登録する。`from __future__ import annotations` と併用すると AttributeError: 'NoneType' object has no attribute '__dict__' で落ちる
created: 2026-08-20
updated: 2026-08-20
---

# importlibの単体ロード + dataclass は sys.modules への事前登録が要る

パッケージの `__init__.py`(重い依存を引く)を避けて、ファイル1つだけを
`importlib.util.spec_from_file_location` で読むとき:

```python
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module   # ← exec より先に置く
spec.loader.exec_module(module)
```

`sys.modules` への登録を省くと、そのモジュールに `@dataclass` があり
かつ `from __future__ import annotations` が効いている場合に

    AttributeError: 'NoneType' object has no attribute '__dict__'

で落ちる(`dataclasses` が型注釈を解決するのに `sys.modules[cls.__module__]` を引くため)。
落ちるのはimport時なので、そのモジュールを読む全テストが一斉に赤くなる。

実例: duocadで `duocad/catalog.py`(純データの宣言)を、OCPを読み込む
`duocad/__init__.py` を経由せずに検査したくて踏んだ。
