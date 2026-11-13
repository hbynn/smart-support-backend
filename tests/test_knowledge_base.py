import sys
import types
import threading

try:
    import chromadb
except ModuleNotFoundError:
    sys.modules['chromadb'] = types.ModuleType('chromadb')

from tooling.knowledge_base import KnowledgeBase


class FakeCollection:
    def __init__(self):
        self.rows = {}
        self.calls = []

    def upsert(self, **kwargs):
        self.calls.append(kwargs)
        for i, doc, meta in zip(kwargs['ids'], kwargs['documents'], kwargs['metadatas']):
            self.rows[i] = (doc, meta)

    def get(self, where, include):
        rows = [(i, d, m) for i, (d, m) in self.rows.items() if all(m.get(k) == v for k, v in where.items())]
        return {'ids': [r[0] for r in rows], 'metadatas': [r[2] for r in rows]}

    def delete(self, ids):
        for i in ids:
            self.rows.pop(i, None)


def make_kb():
    kb = KnowledgeBase.__new__(KnowledgeBase)
    kb._write_lock = threading.RLock()
    kb._collection = FakeCollection()
    return kb


def test_repeated_document_import_is_idempotent():
    kb = make_kb()
    doc = {'title': '退款政策', 'content': '7 天内可以申请退款。'}
    assert kb.add_documents([doc, doc]) == 1
    assert kb.add_documents([doc]) == 1
    assert len(kb._collection.rows) == 1


def test_shorter_document_replaces_chunks_and_legacy_data():
    kb = make_kb()
    kb._collection.rows['legacy'] = ('旧内容', {'title': '政策'})
    kb.add_documents([{'title': '政策', 'content': ('旧规则' * 100 + '。') * 5}])
    assert len(kb._collection.rows) > 1
    assert 'legacy' not in kb._collection.rows
    kb.add_documents([{'title': '政策', 'content': '新的政策'}])
    assert len(kb._collection.rows) == 1
    assert next(iter(kb._collection.rows.values()))[0] == '新的政策'


def test_explicit_ids_allow_documents_with_same_title():
    kb = make_kb()
    kb.add_documents([{'document_id': 'a', 'title': '政策', 'content': '甲'},
                      {'document_id': 'b', 'title': '政策', 'content': '乙'}])
    assert len(kb._collection.rows) == 2
    kb.add_documents([{'document_id': 'a', 'title': '改名', 'content': '新甲'}])
    assert len(kb._collection.rows) == 2
    assert {d for d, m in kb._collection.rows.values()} == {'新甲', '乙'}


def test_long_unpunctuated_text_respects_chunk_size():
    kb = make_kb()
    chunks = kb._chunk_text('长' * 1201, chunk_size=500)
    assert [len(c) for c in chunks] == [500, 500, 201]
    assert ''.join(chunks) == '长' * 1201

class SearchCollection(FakeCollection):
    def count(self):
        return len(self.rows)

    def get(self, where=None, include=None):
        if where is not None:
            return super().get(where, include)
        return {"ids": list(self.rows), "documents": [d for d, _ in self.rows.values()],
                "metadatas": [m for _, m in self.rows.values()]}

    def query(self, query_texts, n_results, include):
        # 故意让向量召回把近似错误码排在正确错误码之前。
        keys = list(self.rows)[:n_results]
        return {"ids": [keys], "documents": [[self.rows[k][0] for k in keys]],
                "metadatas": [[self.rows[k][1] for k in keys]]}


def test_hybrid_exact_code_parent_exception_and_replacement():
    kb = make_kb()
    kb._collection = SearchCollection()
    kb.add_documents([{"title": "其他", "content": "E1025 网络故障"},
                      {"title": "售后", "content": "第十二条 退货\nE1024 处理方式。" +
                       "普通商品可以退。" * 85 + "\n例外：活动商品不适用。"}])
    hit = kb.search("E1024", 1)[0]
    assert hit["section"] == "第十二条 退货"
    assert "例外：活动商品不适用。" in hit["evidence"]
    kb.add_documents([{"title": "售后", "content": "第十三条 更新后的政策"}])
    assert all("第十二条" not in r["evidence"] for r in kb.search("E1024", 5))
