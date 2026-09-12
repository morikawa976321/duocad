---
name: svg-comment-double-hyphen-breaks-xml
description: 落とし穴(2026-08-26・duocadのfaviconで実測) — SVGのコメントに2連ハイフンを書くとXMLとして不正になり、画像が無言で壊れる。CSS変数名をコメントに書くと踏む
created: 2026-08-26
updated: 2026-08-26
---

SVGファイルのコメント `<!-- ... -->` の中に**2連ハイフンを書くとXMLのパースが落ちる**(XMLの仕様でコメント内に `--` を置けない)。CSS変数名を説明に書くと自然に踏む。

**壊れ方が無言**なのが厄介で、例外もログも出ず、`<img>` が壊れた画像アイコンになるだけ。HTTPは200で返るしファイルサイズも正常なので、配信側を疑っても何も出ない。

検出は `python -c "import xml.dom.minidom; xml.dom.minidom.parse('favicon.svg')"` で足りる。**手書きSVGを納品する前に1回パースを通すこと**。実ブラウザで表示まで見る検査があれば同時に捕まる(duocadでは実際にこれで捕まえた)。
