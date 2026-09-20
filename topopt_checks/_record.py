"""測定記録の JSON を「1レコード1行」で書き出す。

`json.dumps(..., indent=N)` は1レコードを欄の数だけ行に展開するので、試行ごとの記録が
数万行になり PR の差分が読めなくなる(段階4の記録22件で 174,841 行)。ここは
**コンパクト形が `WIDTH` に収まる辞書・配列は1行にまとめる**書き方で同じ内容を書く
(同じ22件で 13,343 行。`json.loads` した結果は一致する)。

`json.dumps` と同じ値を表し、違うのは空白だけである。読む側(`json.load`)は変えなくてよい。
"""
from __future__ import annotations

import json

#: 1行に収める上限(文字)。段階4の `steps[]` の1件は入れ子の `peel` を含めて最も長く、
#: 実測で 577 文字(記録22件の最長行)。500 では収まらず1件が15行に割れる。
WIDTH = 600


def _key(key) -> str:
    """辞書のキーを `json.dumps` と同じ文字列へ落とす(json は str 以外のキーを文字列化する)。"""
    if isinstance(key, str):
        text = key
    elif key is None:
        text = 'null'
    elif key is True:
        text = 'true'
    elif key is False:
        text = 'false'
    else:
        text = str(key)
    return json.dumps(text, ensure_ascii=False)


def dumps(obj, *, default=None, allow_nan=True, width: int = WIDTH, indent: int = 2, _level: int = 0) -> str:
    """`obj` を JSON 文字列にする。`default` / `allow_nan` は `json.dumps` と同じ意味。"""
    pad = ' ' * (indent * _level)
    compact = json.dumps(obj, ensure_ascii=False, default=default, allow_nan=allow_nan,
                         separators=(', ', ': '))
    if not isinstance(obj, (dict, list)) or len(pad) + len(compact) <= width:
        return compact
    inner = ' ' * (indent * (_level + 1))
    nested = dict(default=default, allow_nan=allow_nan, width=width, indent=indent, _level=_level + 1)
    if isinstance(obj, dict):
        rows = (f'{inner}{_key(k)}: {dumps(v, **nested)}' for k, v in obj.items())
        return '{\n' + ',\n'.join(rows) + '\n' + pad + '}'
    rows = (f'{inner}{dumps(v, **nested)}' for v in obj)
    return '[\n' + ',\n'.join(rows) + '\n' + pad + ']'


def write(path, obj, *, default=None, allow_nan=True) -> None:
    """`path` の親を作って `dumps(obj)` を UTF-8 で書く(末尾に改行を1つ足す)。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dumps(obj, default=default, allow_nan=allow_nan) + '\n', encoding='utf-8')
