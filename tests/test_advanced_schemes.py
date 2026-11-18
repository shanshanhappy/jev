# -*- coding: utf-8 -*-
import csv
import json
import os
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
import flow_rules
import jev_client
import jev_eval
import raw_to_csv
import schemes


def ans(v):
    return {'type': 'noul', 'noul': v}


class FlowRuleTests(unittest.TestCase):
    def test_h_uses_threshold_and_topk(self):
        payload = {
            'scheme': 'H', 'stage': 'leaf',
            'questions': {'q0001': {}, 'q0002': {}, 'q0003': {}},
            'meta': {'flow': {'cat_tau': 0.5, 'cat_k': 2, 'fallback': 'none',
                              'qid_cats': {'q0001': 'A', 'q0002': 'B', 'q0003': 'C'}}}
        }
        gate = {'cat_A': ans(0.9), 'cat_B': ans(0.4), 'cat_C': ans(0.1)}
        selected, info = flow_rules.apply_dynamic_filter(payload, {'gate_cat': gate})
        self.assertEqual(set(selected), {'q0001', 'q0002'})
        self.assertEqual(info['after'], 2)

    def test_h_gate_failure_fallbacks(self):
        payload = {'scheme': 'H', 'stage': 'leaf', 'questions': {'q0001': {}},
                   'meta': {'flow': {'fallback': 'all', 'qid_cats': {'q0001': 'A'}}}}
        selected, info = flow_rules.apply_dynamic_filter(payload, {})
        self.assertEqual(len(selected), 1)
        self.assertEqual(info['fallback_used'], 'all')
        payload['meta']['flow']['fallback'] = 'none'
        selected, _ = flow_rules.apply_dynamic_filter(payload, {})
        self.assertEqual(selected, {})

    def test_speculative_only_reasks_boundary_and_is_inclusive(self):
        payload = {'scheme': 'Speculative', 'stage': 'pass2',
                   'questions': {f'q{i:04d}': {} for i in range(1, 5)},
                   'meta': {'flow': {'lo': 0.2, 'hi': 0.8, 'fallback': 'none'}}}
        first = {'q0001': ans(0.19), 'q0002': ans(0.2), 'q0003': ans(0.8), 'q0004': ans(0.81)}
        selected, info = flow_rules.apply_dynamic_filter(payload, {'pass1': first})
        self.assertEqual(set(selected), {'q0002', 'q0003'})
        self.assertEqual(info['after'], 2)

    def test_speculative_failure_defaults_to_skip(self):
        payload = {'scheme': 'Speculative', 'stage': 'pass2', 'questions': {'q0001': {}},
                   'meta': {'flow': {'fallback': 'none'}}}
        selected, info = flow_rules.apply_dynamic_filter(payload, {})
        self.assertEqual(selected, {})
        self.assertEqual(info['fallback_used'], 'none')


class EmbeddingTests(unittest.TestCase):
    def test_openai_response_cache_and_cosine(self):
        with tempfile.TemporaryDirectory() as td:
            cache = os.path.join(td, 'vectors.json')
            service = embeddings.EmbeddingService('http://mock/v1', 'secret', 'mock-embed', cache, batch_size=2)
            calls = []
            vectors = {'alpha': [1.0, 0.0], 'beta': [0.0, 1.0], 'gamma': [1.0, 1.0]}

            def fake_post(texts):
                calls.append(list(texts))
                return [vectors[t] for t in texts]

            service._post = fake_post
            first = service.embed(['alpha', 'beta', 'gamma'])
            calls_after_first = len(calls)
            second = service.embed(['gamma', 'alpha'])
            self.assertEqual(first[2], second[0])
            self.assertEqual(len(calls), calls_after_first)  # 第二次全部命中缓存
            self.assertTrue(os.path.exists(cache))
            self.assertAlmostEqual(embeddings.cosine(first[0], first[2]), 2 ** -0.5)
            self.assertEqual(service.url, 'http://mock/v1/embeddings')

    def test_rag_scheme_uses_vector_ranking_not_keyword_count(self):
        labels = [
            {'idx': 1, 'cat': 'A', 'sub': '通用', 'label': '语义目标', 'hits': ['完全无关词'], 'excls': []},
            {'idx': 2, 'cat': 'B', 'sub': '通用', 'label': '另一标签', 'hits': ['帖子'], 'excls': []},
        ]
        row = {'full_text': '帖子正文', 'csv_row_no': '1', 'id': '1', 'site_name': '微博', 'url': '',
               'publish_time': '', 'char_count_raw': '4', 'char_count_full': '4'}
        args = types.SimpleNamespace(top_k=1, embedding_base='http://mock', embedding_key='x',
                                     embedding_model='mock', embedding_cache='', embedding_batch_size=32,
                                     min_score=None, budget=55000, model='jev-latest')

        class FakeService:
            def __init__(self, *a): pass
            def embed(self, texts):
                # 第二个标签虽字面命中，但查询向量与第一个标签更接近，必须选第一个。
                if len(texts) > 1:
                    return [[1.0, 0.0], [0.0, 1.0]]
                return [[1.0, 0.0]]

        captured = []
        with mock.patch('embeddings.EmbeddingService', FakeService), \
             mock.patch('schemes.write_payloads', side_effect=lambda ps, scheme: captured.extend(ps)):
            schemes.SchemeRAG.run(labels, {'S001': row}, {'S001': ''}, 2000, args)
        self.assertEqual(len(captured), 1)
        self.assertEqual(set(captured[0]['questions']), {'q0001'})
        self.assertEqual(captured[0]['meta']['rag']['embedding_model'], 'mock')


class ClientIntegrationTests(unittest.TestCase):
    def test_client_h_filters_second_stage_from_real_gate_response(self):
        with tempfile.TemporaryDirectory() as td:
            payload_dir = Path(td) / 'payloads'
            raw_dir = Path(td) / 'raw'
            payload_dir.mkdir()
            base = {'sample': 'S001', 'scheme': 'H', 'mode': 'cascade', 'chunk': 1, 'nchunks': 1,
                    'model': 'jev-latest', 'state': {'内容': 'x'}, 'meta': {}}
            gate = dict(base, stage='gate_cat', questions={'cat_A': {}, 'cat_B': {}})
            leaf = dict(base, stage='leaf', questions={'q0001': {}, 'q0002': {}},
                        meta={'flow': {'cat_tau': 0.5, 'cat_k': 0, 'fallback': 'none',
                                       'qid_cats': {'q0001': 'A', 'q0002': 'B'}}})
            (payload_dir / 'S001__H__cascade__gate_cat.jsonl').write_text(json.dumps(gate) + '\n', encoding='utf-8')
            (payload_dir / 'S001__H__cascade__leaf.jsonl').write_text(json.dumps(leaf) + '\n', encoding='utf-8')
            bodies = []

            def fake_post(base_url, key, body, timeout, long_context=False, endpoint=''):
                bodies.append(body)
                if 'cat_A' in body['questions']:
                    answers = {'cat_A': ans(0.9), 'cat_B': ans(0.1)}
                else:
                    answers = {qid: ans(0.7) for qid in body['questions']}
                return 200, {'model': 'mock', 'answers': answers,
                             'usage': {'input_tokens': 10, 'output_tokens': 0}}, 1

            argv = ['jev_client.py', '--dir', str(payload_dir), '--all', '--scheme', 'H',
                    '--raw-dir', str(raw_dir), '--key', 'x', '--base', 'http://mock',
                    '--retry', '1', '--sleep', '0', '--no-csv']
            with mock.patch.object(jev_client, 'ROOT', td), \
                 mock.patch.object(jev_client, 'post_once', side_effect=fake_post), \
                 mock.patch.object(sys, 'argv', argv):
                self.assertFalse(jev_client.main())
            self.assertEqual(len(bodies), 2)
            self.assertEqual(set(bodies[1]['questions']), {'q0001'})
            records = [json.loads(x) for x in (raw_dir / 'S001__H__cascade.jsonl').read_text(encoding='utf-8').splitlines()]
            self.assertEqual(records[1]['flow']['after'], 1)
            self.assertEqual(records[0]['run_id'], records[1]['run_id'])


class ResultMergeTests(unittest.TestCase):
    def test_eval_merges_pass2_and_ignores_previous_run(self):
        with tempfile.TemporaryDirectory() as td:
            fp = Path(td) / 'S001__Speculative__speculative.jsonl'
            records = [
                {'sample': 'S001', 'scheme': 'Speculative', 'mode': 'speculative', 'stage': 'pass1',
                 'run_id': 'old', 'ts': '2026-01-01T00:00:00', 'http': 200, 'base': '', 'latency_ms': 1,
                 'body': {'answers': {'q0001': ans(0.9)}, 'usage': {'input_tokens': 1}}},
                {'sample': 'S001', 'scheme': 'Speculative', 'mode': 'speculative', 'stage': 'pass1',
                 'run_id': 'new', 'ts': '2026-01-02T00:00:00', 'http': 200, 'base': '', 'latency_ms': 1,
                 'body': {'answers': {'q0001': ans(0.2), 'q0002': ans(0.7)}, 'usage': {'input_tokens': 2}}},
                {'sample': 'S001', 'scheme': 'Speculative', 'mode': 'speculative', 'stage': 'pass2',
                 'run_id': 'new', 'ts': '2026-01-02T00:00:01', 'http': 200, 'base': '', 'latency_ms': 1,
                 'body': {'answers': {'q0001': ans(0.8)}, 'usage': {'input_tokens': 3}}},
            ]
            fp.write_text(''.join(json.dumps(r) + '\n' for r in records), encoding='utf-8')
            with mock.patch.object(jev_eval, 'D_R', td):
                probs, meta = jev_eval.load_probs('Speculative', 'speculative')
            self.assertEqual(probs['S001'], {1: 0.8, 2: 0.7})
            self.assertEqual(meta['S001']['in_tok'], 5)

    def test_zero_candidate_sample_is_kept(self):
        with tempfile.TemporaryDirectory() as td:
            fp = Path(td) / 'S001__H__cascade.jsonl'
            rec = {'sample': 'S001', 'scheme': 'H', 'mode': 'cascade', 'stage': 'leaf', 'run_id': 'r1',
                   'ts': '2026-01-01T00:00:00', 'http': 200, 'skipped': True, 'base': '',
                   'body': {'answers': {}, 'usage': {'input_tokens': 0}}}
            fp.write_text(json.dumps(rec) + '\n', encoding='utf-8')
            with mock.patch.object(jev_eval, 'D_R', td):
                probs, _ = jev_eval.load_probs('H', 'cascade')
            self.assertIn('S001', probs)
            self.assertEqual(probs['S001'], {})


    def test_raw_csv_speculative_exports_final_probability_once(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            raw_dir, payload_dir, out_dir = root / 'raw', root / 'payloads', root / 'out'
            raw_dir.mkdir(); payload_dir.mkdir()
            taxonomy = root / 'labels.csv'
            with taxonomy.open('w', encoding='utf-8-sig', newline='') as f:
                w = csv.DictWriter(f, fieldnames=['标签问题ID', '类别', '细分类别', 'TOPIC ANGLE', '标签层级'])
                w.writeheader()
                for i in (1, 2):
                    w.writerow({'标签问题ID': f'q{i:04d}', '类别': '测试', '细分类别': '通用',
                                'TOPIC ANGLE': f'标签{i}', '标签层级': '叶子标签'})
            for stage, text in [('pass1', '第一轮'), ('pass2', '第二轮')]:
                payload = {'questions': {'q0001': {'instructions': text + '1'},
                                         'q0002': {'instructions': text + '2'}}}
                (payload_dir / f'S001__Speculative__speculative__{stage}.jsonl').write_text(
                    json.dumps(payload) + '\n', encoding='utf-8')
            recs = [
                {'sample': 'S001', 'scheme': 'Speculative', 'mode': 'speculative', 'stage': 'pass1',
                 'chunk': 1, 'run_id': 'r1', 'ts': '2026-01-01T00:00:00', 'http': 200,
                 'body': {'answers': {'q0001': ans(0.7), 'q0002': ans(0.9)},
                          'usage': {'input_tokens': 10, 'output_tokens': 0}}},
                {'sample': 'S001', 'scheme': 'Speculative', 'mode': 'speculative', 'stage': 'pass2',
                 'chunk': 1, 'run_id': 'r1', 'ts': '2026-01-01T00:00:01', 'http': 200,
                 'body': {'answers': {'q0001': ans(0.4)}, 'usage': {'input_tokens': 5, 'output_tokens': 0}}},
            ]
            (raw_dir / 'S001__Speculative__speculative.jsonl').write_text(
                ''.join(json.dumps(r) + '\n' for r in recs), encoding='utf-8')
            with mock.patch.object(raw_to_csv, 'LABEL_FILE', str(taxonomy)):
                raw_to_csv.export(threshold=0.6, raw_dir=str(raw_dir), payload_dir=str(payload_dir),
                                  outdir=str(out_dir), quiet=True)
            with (out_dir / '打标明细_全量.csv').open(encoding='utf-8-sig', newline='') as f:
                rows = list(csv.DictReader(f))
            self.assertEqual(len(rows), 2)
            by_qid = {r['问题ID']: r for r in rows}
            self.assertEqual(float(by_qid['q0001']['概率/取值']), 0.4)
            self.assertEqual(by_qid['q0001']['是否命中'], 'N')
            self.assertEqual(by_qid['q0002']['是否命中'], 'Y')
            self.assertEqual(by_qid['q0001']['问题原文'], '第二轮1')
            self.assertEqual(by_qid['q0002']['问题原文'], '第一轮2')
            self.assertEqual(int(by_qid['q0001']['input_tokens']), 15)


    def test_eval_output_dir_follows_raw_dir_and_can_be_overridden(self):
        raw = str(Path(tempfile.gettempdir()) / 'example_raw_H')
        override = str(Path(tempfile.gettempdir()) / 'example_eval')
        self.assertEqual(jev_eval.resolve_output_dir('', raw), os.path.abspath(raw))
        self.assertEqual(jev_eval.resolve_output_dir(override, raw), os.path.abspath(override))
        self.assertEqual(jev_eval.resolve_output_dir('', ''), os.path.join(jev_eval.ROOT, 'out'))



if __name__ == '__main__':
    unittest.main()
