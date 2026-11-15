import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from rag_db import MODEL_DIMENSION, PROJECT_SPACE, RagDatabase
from rag_lexical import LexicalIndex
from rag_navigation import navigate, plan_query


def chunk(n, text, *, scope="campus", document_id="a" * 64, source="制度汇编"):
    return {"chunk_id": f"{n:064x}", "document_id": document_id, "source": source,
            "scope": scope, "chunk_index": 0, "text": text}


class LexicalIndexTests(unittest.TestCase):
    def test_searches_short_chinese_and_exact_numbers_in_the_requested_scope(self):
        with tempfile.TemporaryDirectory() as temp:
            index = LexicalIndex(Path(temp, "index.sqlite3"))
            first = chunk(1, "新生注册。样校学字〔2031〕731号。")
            private = chunk(2, "新生注册。样校学字〔2031〕731号。", scope="Campus")
            outdated = chunk(3, "样校学字〔2030〕731号。")
            self.assertEqual(index.rebuild([first, private, outdated]), 3)
            with index.reader() as reader:
                self.assertEqual(reader.lookup("campus", "新生", limit=5), [first["chunk_id"]])
                self.assertEqual(reader.lookup("Campus", "新生", limit=5), [private["chunk_id"]])
                self.assertEqual(reader.lookup("campus", "样校学字[2031]731号", phrase=True, limit=5), [first["chunk_id"]])
                self.assertEqual(reader.lookup("campus", "（2031）第0731号", phrase=True, limit=5), [first["chunk_id"]])
                self.assertEqual(reader.lookup("unknown", '" OR 注册', limit=5), [])

    def test_partial_rebuild_keeps_old_index_and_dirty_writes_are_never_served(self):
        with tempfile.TemporaryDirectory() as temp:
            index = LexicalIndex(Path(temp, "index.sqlite3"))
            before = chunk(1, "旧记录只包含羽毛球。样校学字〔2032〕409号。")
            after = chunk(2, "新记录已替换为篮球。")
            index.rebuild([before])

            def broken_rows():
                yield after
                raise RuntimeError("interrupted")

            with self.assertRaises(RuntimeError):
                index.rebuild(broken_rows())
            with index.reader() as reader:
                self.assertEqual(reader.lookup("campus", "羽毛球", limit=5), [before["chunk_id"]])
            self.assertTrue(index.invalidate())
            with index.reader() as reader:
                self.assertIsNone(reader)
            index.replace_document("campus", before["document_id"], [after])
            with index.reader() as reader:
                self.assertEqual(reader.lookup("campus", "羽毛球", limit=5), [])
                self.assertEqual(reader.lookup("campus", "[2032]第0409号", phrase=True, limit=5), [])
                self.assertEqual(reader.lookup("campus", "篮球", limit=5), [after["chunk_id"]])
            self.assertTrue(index.invalidate())
            index.replace_document("campus", after["document_id"], [])
            with index.reader() as reader:
                self.assertEqual(reader.lookup("campus", "篮球", limit=5), [])

    def test_milvus_hydrates_lexical_hits_with_scope_and_missing_index_uses_dense(self):
        anchor = chunk(1, "样校学字〔2031〕731号。第一条。")
        row = {k: v for k, v in anchor.items() if k != "scope"}
        vector = [1.0] + [0.0] * (MODEL_DIMENSION - 1)
        with tempfile.TemporaryDirectory() as temp, patch("rag_db.MilvusClient") as connection:
            db = RagDatabase(Path(temp, "test.db"))
            db._retrieval_mode = "adaptive"
            db._lexical.rebuild([anchor])
            client = connection.return_value
            client.query.side_effect = lambda *a, **kw: [{**row, "vector": vector}]
            client.search.return_value = [[{"id": anchor["chunk_id"], "distance": 1.0, "entity": row}]]
            with patch.object(db, "_metadata", return_value=PROJECT_SPACE):
                result = db._search_project_vector(vector, scope="campus", top_k=1, query="样校学字〔2031〕731号")
                self.assertEqual(result, [{**row, "score": 1.0}])
                self.assertIn('scope == "campus" and chunk_id in ', client.query.call_args.kwargs["filter"])
                client.search.assert_not_called()
                db._lexical.invalidate()
                self.assertEqual(db._search_project_vector(vector, scope="campus", top_k=1,
                                                           query="样校学字〔2031〕731号"), result)
                client.search.assert_called_once()
                client.query.assert_called_once()


class NavigationTests(unittest.TestCase):
    def test_number_retrieval_does_not_confuse_issuing_units(self):
        direct = {**chunk(1, "样校学字〔2032〕0409号。第一条。"), "score": 0.71}
        other = {**chunk(2, "样校教字〔2032〕409号。第一条。"), "score": 0.81}
        dense = Mock(return_value=[other])
        hits = {entry["chunk_id"]: entry for entry in (direct, other)}
        result = navigate("样校学字〔2032〕409号", 1, dense, lambda *a: list(reversed(hits)),
                          lambda ids: [hits[key] for key in ids])
        self.assertEqual(result, [direct])

    def test_disconnected_facets_do_not_compete_for_the_same_first_results(self):
        question = "本科转专业怎么申请？顺便问图书馆古籍阅览怎么预约？"
        course = {**chunk(1, "本科转专业申请。按教务通知提交。"), "score": 0.49}
        library = {**chunk(2, "图书馆古籍阅览预约。读者服务页面提交申请。"), "score": 0.73}
        repetition = {**library, "chunk_id": f"{3:064x}"}
        known = {hit["chunk_id"]: hit for hit in (course, library, repetition)}
        queries = []
        dense = Mock(return_value=[library, repetition])

        def lexical(text, phrase):
            queries.append((text, phrase))
            if "转专业" in text and "古籍" not in text:
                return [course["chunk_id"]]
            return [library["chunk_id"], repetition["chunk_id"]]

        result = navigate(question, 2, dense, lexical, lambda ids: [known[key] for key in ids])
        self.assertEqual({item["chunk_id"] for item in result}, {course["chunk_id"], library["chunk_id"]})
        dense.assert_called_once()
        self.assertLessEqual(len(set(queries)), 4)
        self.assertEqual(len(set(queries)), len(queries))

    def test_clarification_to_find_the_source_is_not_a_separate_topic(self):
        plan = plan_query("新生报到前，需要准备哪些材料？请帮我找相关的官方说明。")
        self.assertEqual(len(plan.facets), 1)
        self.assertIn("新生报到", plan.focus)
        self.assertIn("材料", plan.focus)


if __name__ == "__main__":
    unittest.main()
