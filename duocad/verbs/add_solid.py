"""生成ソリッドをB-rep本文ごと1操作で保存する。外部ファイルに依存しない。"""
from ..document import Body, new_body_id
from ..kernel_brep import encode_brep, decode_brep
from ..oplog import register_verb


def add_solid_body(document, shape, name):
    text = encode_brep(shape)
    restored = decode_brep(text)
    body_id = new_body_id()
    document.add_body(Body(id=body_id, name=name, kind='solid', shape=restored))
    document.log.append('add_solid_body', dict(body_id=body_id, name=name, brep=text))
    return body_id


def _do(document, operation):
    args = operation.args
    shape = decode_brep(args['brep'])
    document.add_body(Body(id=args['body_id'], name=args['name'], kind='solid', shape=shape))


def _undo(document, operation):
    document.remove_added_body(operation.args['body_id'])


register_verb('add_solid_body', do=_do, undo=_undo)
