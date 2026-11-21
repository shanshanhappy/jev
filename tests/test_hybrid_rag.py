# -*- coding: utf-8 -*-
import csv
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'core'))
sys.path.append(str(ROOT / 'tools'))

import embeddings
import jev_client
import jev_eval
import raw_to_csv
import schemes
from hybrid_rag import merge_candidates, strong_regex_detail
from evaluate_candidate_recall_v023 import evaluate


def label(idx, name, hits=None):
    return {'idx': idx, 'cat': '测试', 'sub': '通用', 'label': name,
            'hits': hits or [], 'ands': [], 'excls': [], 'poss': [], 'negs': [], 'near': 15}


class CandidateMergeTests(unittest.TestCase):
    def test_regex_candidate_survives_topk_and_overlap_is_deduplicated(self):
        labels = [label(1, '语义1'), label(2, '字面'), label(3, '语义3')]
        scored = [(0.9, labels[0]), (0.8, labels[2]), (0.7, labels[1])]
        regex = {1: {'matched': False}, 2: {'matched': True, 'hit': '字面'}, 3: {'matched': False}}
        top1 = merge_candidates(regex, scored, 1)
        self.assertEqual([c['label']['idx'] for c in top1], [1, 2])
        self.assertEqual(top1[0]['source'], 'rag')
        self.assertEqual(top1[1]['source'], 'regex')
        all3 = merge_candidates(regex, scored, 3)
        self.assertEqual(len(all3), 3)
        self.assertEqual(all3[1]['source'], 'both')
        self.assertEqual(all3[1]['rag_rank'], 3)

    def test_min_score_does_not_remove_regex_hit(self):
        labels = [label(1, '字面'), label(2, '低分语义')]
        scored = [(0.1, labels[1]), (0.0, labels[0])]
        regex = {1: {'matched': True}, 2: {'matched': False}}
        selected = merge_candidates(regex, scored, 2, min_score=0.5)
        self.assertEqual([c['label']['idx'] for c in selected], [1])


class StrongRegexTests(unittest.TestCase):
    def test_exact_clean_label_is_direct_but_synonym_is_not(self):
        item = label(1, '保湿', ['滋润'])
        item['excls'] = ['不']
        clean = strong_regex_detail('这款保湿很好', item)
        self.assertTrue(clean['direct'])
        synonym = strong_regex_detail('这款很滋润', item)
        self.assertFalse(synonym['matched'])
        self.assertFalse(synonym['direct'])

    def test_negated_hit_is_review_and_later_clean_hit_wins(self):
        item = label(1, '保湿')
        item['excls'] = ['不']
        review = strong_regex_detail('不是不保湿', item)
        self.assertTrue(review['matched'])
        self.assertFalse(review['direct'])
        self.assertEqual(review['status'], 'review')
        clean = strong_regex_detail('不保湿。' + '隔开' * 12 + '保湿很好', item)
        self.assertTrue(clean['direct'])
        self.assertGreater(clean['position'], 3)


class SchemeTests(unittest.TestCase):
    def setUp(self):
        self.labels = [label(1, '语义目标', ['无关词']), label(2, '字面目标', ['明确词'])]
        self.row = {'full_text': '帖子含字面目标', 'csv_row_no': '1', 'id': 'post-1',
                    'site_name': '微博', 'url': '', 'publish_time': '',
                    'char_count_raw': '7', 'char_count_full': '7'}
        self.args = types.SimpleNamespace(
            top_k=1, candidate_scan_k=2, embedding_base='http://mock', embedding_key='x',
            embedding_model='mock', embedding_cache='', embedding_batch_size=32,
            min_score=None, budget=55000, model='jev-latest', excls_scope='window')

    def test_scheme_generates_union_with_source_and_a2_questions(self):
        queries = []

        class FakeService:
            def __init__(self, *args): pass
            def embed(self, texts):
                if len(texts) > 1:
                    return [[1.0, 0.0], [0.0, 1.0]]
                queries.extend(texts)
                return [[1.0, 0.0]]
            def usage_snapshot(self):
                return {'requests': 2, 'input_tokens': 12}

        captured = []
        with tempfile.TemporaryDirectory() as td, \
             mock.patch('embeddings.EmbeddingService', FakeService), \
             mock.patch.object(schemes, 'OUTD', td), \
             mock.patch('schemes.write_payloads', side_effect=lambda ps, scheme: captured.extend(ps)):
            schemes.SchemeHybridRAG.run(self.labels, {'S001': self.row}, {'S001': ''}, 2000, self.args)
            audit = Path(td) / '候选标签_HybridRAG.csv'
            usage = Path(td) / 'embedding_usage_HybridRAG.json'
            self.assertTrue(audit.exists())
            self.assertTrue(usage.exists())
            with audit.open(encoding='utf-8-sig', newline='') as f:
                rows = list(csv.DictReader(f))
            self.assertEqual({r['标签'] for r in rows}, {'语义目标', '字面目标'})
            by_label = {r['标签']: r for r in rows}
            self.assertEqual(by_label['语义目标']['是否送问'], 'Y')
            self.assertEqual(by_label['字面目标']['是否送问'], 'N')
            self.assertEqual(by_label['字面目标']['是否直接打标'], 'Y')

        self.assertEqual(queries, ['帖子含字面目标'])
        self.assertEqual(len(captured), 1)
        self.assertEqual(set(captured[0]['questions']), {'q0001'})
        self.assertEqual(captured[0]['scheme'], 'HybridRAG')
        self.assertEqual(captured[0]['mode'], 'hybrid_rag')
        self.assertIn('常见说法', captured[0]['questions']['q0001']['instructions'])
        flow = captured[0]['meta']['hybrid_rag']
        meta = flow['candidates']
        self.assertEqual(meta['q0001']['source'], 'rag')
        self.assertNotIn('q0002', meta)
        self.assertTrue(flow['direct_labels']['q0002']['direct'])

    def test_zero_candidate_still_writes_skippable_payload(self):
        for item in self.labels:
            item['hits'] = []
        self.row = dict(self.row, full_text='完全无关')
        self.args.min_score = 2.0

        class FakeService:
            def __init__(self, *args): pass
            def embed(self, texts):
                return [[1.0, 0.0] for _ in texts] if len(texts) > 1 else [[1.0, 0.0]]
            def usage_snapshot(self): return {}

        captured = []
        with tempfile.TemporaryDirectory() as td, \
             mock.patch('embeddings.EmbeddingService', FakeService), \
             mock.patch.object(schemes, 'OUTD', td), \
             mock.patch('schemes.write_payloads', side_effect=lambda ps, scheme: captured.extend(ps)):
            schemes.SchemeHybridRAG.run(self.labels, {'S001': self.row}, {'S001': ''}, 2000, self.args)
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0]['questions'], {})


class DirectResultIntegrationTests(unittest.TestCase):
    def test_direct_and_jev_labels_merge_into_eval_and_csv(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            payload_dir, raw_dir, out_dir = root/'payloads', root/'raw', root/'out'
            payload_dir.mkdir()
            payload = {
                'sample': 'S001', 'scheme': 'HybridRAG', 'mode': 'hybrid_rag',
                'stage': 'leaf', 'chunk': 1, 'nchunks': 1, 'model': 'jev-latest',
                'state': {'内容': '保湿，语义修护'}, 'questions': {'q0001': {'instructions': 'Jev判断'}},
                'meta': {'hybrid_rag': {'direct_labels': {
                    'q0002': {'matched': True, 'direct': True, 'status': 'direct',
                              'hit': '保湿', 'position': 0, 'evidence': '保湿'}}}}
            }
            (payload_dir/'S001__HybridRAG__hybrid_rag.jsonl').write_text(
                json.dumps(payload, ensure_ascii=False)+'\n', encoding='utf-8')

            def fake_post(*args, **kwargs):
                return 200, {'model': 'mock', 'answers': {'q0001': {'type': 'noul', 'noul': 0.8}},
                             'usage': {'input_tokens': 10, 'output_tokens': 0}}, 1

            argv = ['jev_client.py', '--dir', str(payload_dir), '--all', '--scheme', 'HybridRAG',
                    '--raw-dir', str(raw_dir), '--key', 'x', '--base', 'http://mock',
                    '--retry', '1', '--sleep', '0', '--no-csv']
            with mock.patch.object(jev_client, 'ROOT', td), \
                 mock.patch.object(jev_client, 'post_once', side_effect=fake_post), \
                 mock.patch.object(sys, 'argv', argv):
                self.assertFalse(jev_client.main())
            raw_file = raw_dir/'S001__HybridRAG__hybrid_rag.jsonl'
            records = [json.loads(x) for x in raw_file.read_text(encoding='utf-8').splitlines()]
            self.assertEqual([r['stage'] for r in records], ['regex_direct', 'leaf'])
            with mock.patch.object(jev_eval, 'D_R', str(raw_dir)):
                probs, meta = jev_eval.load_probs('HybridRAG', 'hybrid_rag')
            self.assertEqual(probs['S001'], {1: 0.8, 2: 1.0})
            self.assertEqual(meta['S001']['in_tok'], 10)

            taxonomy = root/'labels.csv'
            with taxonomy.open('w', encoding='utf-8-sig', newline='') as f:
                writer = csv.DictWriter(f, fieldnames=['标签问题ID', '类别', '细分类别', 'TOPIC ANGLE', '标签层级'])
                writer.writeheader()
                writer.writerows([
                    {'标签问题ID': 'q0001', '类别': '测试', '细分类别': '通用', 'TOPIC ANGLE': '修护', '标签层级': '叶子'},
                    {'标签问题ID': 'q0002', '类别': '测试', '细分类别': '通用', 'TOPIC ANGLE': '保湿', '标签层级': '叶子'}])
            with mock.patch.object(raw_to_csv, 'LABEL_FILE', str(taxonomy)):
                raw_to_csv.export(threshold=0.6, raw_dir=str(raw_dir), payload_dir=str(payload_dir),
                                  outdir=str(out_dir), quiet=True)
            with (out_dir/'打标明细.csv').open(encoding='utf-8-sig', newline='') as f:
                rows = list(csv.DictReader(f))
            self.assertEqual({r['问题ID'] for r in rows}, {'q0001', 'q0002'})
            self.assertEqual({r['阶段'] for r in rows}, {'final'})


class LabelDocumentTests(unittest.TestCase):
    def test_rag_document_ignores_category_and_subcategory(self):
        doc = embeddings.label_document({'label': '保湿', 'cat': '错误类别', 'sub': '错误细分类别',
                                         'hits': ['补水', '滋润']})
        self.assertIn('保湿', doc)
        self.assertIn('补水', doc)
        self.assertNotIn('错误类别', doc)
        self.assertNotIn('错误细分类别', doc)


class UsageTests(unittest.TestCase):
    def test_embedding_usage_and_cache_hits_are_counted(self):
        with tempfile.TemporaryDirectory() as td:
            service = embeddings.EmbeddingService('http://mock/v1', 'x', 'mock',
                                                  str(Path(td) / 'cache.json'), batch_size=2)
            vectors = {'a': [1.0], 'b': [2.0], 'c': [3.0]}
            def fake_post(texts):
                service.last_usage = {'total_tokens': len(texts) * 2}
                return [vectors[t] for t in texts]
            service._post = fake_post
            service.embed(['a', 'b', 'c'])
            service.embed(['a', 'c'])
            stats = service.usage_snapshot()
            self.assertEqual(stats['requests'], 2)
            self.assertEqual(stats['texts_sent'], 3)
            self.assertEqual(stats['cache_hits'], 2)
            self.assertEqual(stats['input_tokens'], 6)


class CandidateEvaluationTests(unittest.TestCase):
    def test_recall_scans_topk_and_keeps_zero_candidate_post(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            candidate = root / 'candidates.csv'
            with candidate.open('w', encoding='utf-8-sig', newline='') as f:
                fields = ['样本号', '帖子ID', '标签', '正则命中', 'RAG排名', '是否直接打标']
                writer = csv.DictWriter(f, fieldnames=fields); writer.writeheader()
                writer.writerows([
                    {'样本号': 'S001', '帖子ID': 'p1', '标签': 'A', '正则命中': 'Y', 'RAG排名': 5, '是否直接打标': 'Y'},
                    {'样本号': 'S001', '帖子ID': 'p1', '标签': 'B', '正则命中': 'N', 'RAG排名': 1},
                    {'样本号': 'S001', '帖子ID': 'p1', '标签': 'C', '正则命中': 'N', 'RAG排名': 20},
                    {'样本号': 'S002', '帖子ID': 'p2', '标签': '', '正则命中': 'N', 'RAG排名': ''},
                ])
            overview = root / 'overview.csv'
            with overview.open('w', encoding='utf-8-sig', newline='') as f:
                writer = csv.DictWriter(f, fieldnames=['样本号', '帖子ID（文本）']); writer.writeheader()
                writer.writerows([{'样本号': 'P1', '帖子ID（文本）': 'p1'},
                                  {'样本号': 'P2', '帖子ID（文本）': 'p2'}])
            evidence = root / 'evidence.csv'
            with evidence.open('w', encoding='utf-8-sig', newline='') as f:
                writer = csv.DictWriter(f, fieldnames=['样本号', '标签']); writer.writeheader()
                for p_id, name in [('P1', 'A'), ('P1', 'A'), ('P1', 'B'), ('P1', 'C'),
                                   ('P1', 'D'), ('P2', 'E')]:
                    writer.writerow({'样本号': p_id, '标签': name})
            result = evaluate(str(candidate), str(overview), str(evidence), str(root / 'out'), [1, 20])
            self.assertEqual(result['posts'], 2)
            top1, top20 = result['rows']
            self.assertEqual(top1['标准答案标签总数'], 5)
            self.assertEqual(top1['强正则直接标签数'], 1)
            self.assertEqual(top1['强正则直接命中数'], 1)
            self.assertAlmostEqual(top1['强正则直接准确率'], 1.0)
            self.assertEqual(top1['并集候选召回数'], 2)
            self.assertAlmostEqual(top1['并集候选召回率'], 0.4)
            self.assertEqual(top1['仅正则补回'], 1)
            self.assertEqual(top1['仅RAG补回'], 1)
            self.assertEqual(top20['并集候选召回数'], 3)
            self.assertAlmostEqual(top20['并集候选召回率'], 0.6)
            self.assertEqual(top20['并集漏标数'], 2)


if __name__ == '__main__':
    unittest.main()
