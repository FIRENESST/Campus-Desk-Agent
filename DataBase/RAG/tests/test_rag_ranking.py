import unittest
from unittest.mock import Mock, patch

from rag_db import MODEL_DIMENSION, PROJECT_SPACE, RagDatabase
from rag_ranking import rank_hits


def hit(chunk_id, score, text, source="校园资料"):
    return {"chunk_id": chunk_id, "score": score, "text": text, "source": source,
            "document_id": chunk_id, "chunk_index": 0}


class RagRankingTests(unittest.TestCase):
    def test_local_match_disambiguates_related_awards(self):
        graduate = hit("graduate", 0.72, "研究生国家奖学金评审条件及申请流程。")
        other = hit("other", 0.69, "本科生国家励志奖学金申请条件和资格认定。")
        policy = hit("policy", 0.66, "本科生国家奖学金评审条件：申请人应满足以下条件。", "本科生国家奖学金实施办法")

        ranked = rank_hits("本科生国家奖学金 评审条件 申请", [graduate, other, policy], 2)

        self.assertIs(ranked[0], policy)
        self.assertEqual(ranked[0]["score"], 0.66)

    def test_passage_with_conditions_together_beats_scattered_matches(self):
        drift = hit("drift", 0.70, "录取通知书可以作为申请材料。" + "其他说明。" * 45 + "核对居民身份证。")
        rule = hit("rule", 0.65, "新生报到时备好录取通知书和有效身份证件。")

        ranked = rank_hits("录取通知书 身份证", [drift, rule], 1)

        self.assertEqual(ranked, [rule])

    def test_near_duplicates_leave_room_for_another_section(self):
        first = hit("first", 0.79, "新生入学时应凭录取通知书和有效身份证件办理注册。")
        repeated = hit("repeated", 0.78, "新生入学时应凭录取通知书和有效身份证件办理注册。详见第三条。", "旧版校园资料")
        another = hit("another", 0.74, "新生如有特殊情况不能按期入学，请事先联系所在学院。")

        ranked = rank_hits("新生 入学", [first, repeated, another], 3)

        self.assertEqual(ranked, [first, another, repeated])

    def test_vector_and_text_entry_points_use_one_scoped_search(self):
        first = hit("first", 0.73, "研究生国家奖学金评审条件及申请流程。")
        best = hit("best", 0.68, "本科生国家奖学金评审条件：申请人应满足以下条件。")
        candidates = [
            {"id": item["chunk_id"], "distance": item["score"],
             "entity": {key: value for key, value in item.items() if key not in {"chunk_id", "score"}}}
            for item in (first, best)
        ]
        vector = [1.0] * MODEL_DIMENSION
        with patch("rag_db.MilvusClient", return_value=Mock()) as connection:
            database = RagDatabase("http://127.0.0.1:19530")
        client = connection.return_value
        client.search.side_effect = lambda **options: [candidates[:options["limit"]]]
        with patch.object(database, "_metadata", return_value=PROJECT_SPACE):
            query = "本科生国家奖学金 评审条件 申请"
            queries = (
                (database._search_project_vector, {"vector": vector, "query": query}, best),
                (database.search, {"query": query}, best),
                (database._search_project_vector, {"vector": vector}, first),
            )
            for find, args, expected in queries:
                with self.subTest(find=find.__name__, ranked="query" in args), patch("rag_db.project_query_embedding", return_value=vector):
                    self.assertEqual(find(**args, scope="campus", top_k=1), [expected])
                    client.search.assert_called_once()
                    self.assertEqual(client.search.call_args.kwargs["filter"], 'scope == "campus"')
                    self.assertEqual(client.search.call_args.kwargs["limit"], 12 if "query" in args else 1)
                    client.search.reset_mock()


if __name__ == "__main__":
    unittest.main()
