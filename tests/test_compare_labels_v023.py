# -*- coding: utf-8 -*-
import csv
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import compare_labels_v023 as cmp


class CompareLabelsV023Tests(unittest.TestCase):
    def write_csv(self, path, header, rows, prefix=None):
        with open(path, 'w', encoding='utf-8-sig', newline='') as f:
            if prefix:
                f.write(prefix)
            w = csv.writer(f)
            w.writerow(header)
            w.writerows(rows)

    def test_post_id_match_dedup_zero_prediction_and_taxonomy_diagnostic(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            summary = root / 'pred_summary.csv'
            detail = root / 'pred_detail.csv'
            gold_ov = root / 'gold_ov.csv'
            gold_ev = root / 'gold_ev.csv'
            # 故意让标准答案顺序与预测不同，验证不是按行号匹配。
            self.write_csv(summary, ['样本号', '帖子ID（文本）'], [
                ['S001', 'post-A'], ['S002', 'post-B'], ['S003', 'post-missing']])
            self.write_csv(detail, ['样本号', '标签', '类别', '细分类别', '命中阈值'], [
                ['S001', '紧致', '效果表现', '抗老', '0.9'],
                ['S001', '紧致', '效果表现', '抗老', '0.9'],  # 重复预测只算一次
                ['S001', '毛孔', '皮肤问题', '修护', '0.9'],
                ['S999', '无归属', '测试', '测试', '0.9'],
            ])
            self.write_csv(gold_ov, ['样本号', '帖子ID（文本）'], [
                ['P000002', 'post-B'], ['P000001', 'post-A']], prefix='标题\n说明\n')
            self.write_csv(gold_ev, ['样本号', '标签', '类别', '细分类别'], [
                ['P000001', '紧致', '功效', '抗老'],
                ['P000001', '紧致', '功效', '抗老'],  # 多证据只算一次
                ['P000001', '变白', '功效', '美白'],
                ['P000002', '保湿补水', '功效', '修护'],
            ], prefix='标题\n说明\n')
            result = cmp.compare(str(detail), str(summary), str(gold_ov), str(gold_ev), td)
            self.assertEqual(result['samples'], 2)  # S003无法匹配，不纳入指标
            self.assertEqual(result['anomalies'], 2)  # S003 + S999
            # S001：紧致TP、毛孔FP、变白FN；S002：0预测，保湿补水FN
            self.assertEqual((result['tp'], result['fp'], result['fn']), (1, 1, 2))
            with open(result['detail'], encoding='utf-8-sig', newline='') as f:
                rows = list(csv.DictReader(f))
            self.assertEqual(len(rows), 4)
            tight = next(r for r in rows if r['标签'] == '紧致')
            self.assertEqual(tight['标准答案样本号'], 'P000001')
            self.assertEqual(list(tight), ['帖子ID', '预测样本号', '标准答案样本号', '标签', '对比结果'])
            zero = next(r for r in rows if r['预测样本号'] == 'S002')
            self.assertEqual(zero['对比结果'], 'FN')  # 0预测样本仍算漏标
            self.assertTrue(result['report'].endswith('tau_0_9.md'))

    def test_duplicate_post_id_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for name, header, rows in [
                ('summary.csv', ['样本号', '帖子ID（文本）'], [['S001', 'A']]),
                ('detail.csv', ['样本号', '标签'], [['S001', 'X']]),
                ('gold_ov.csv', ['样本号', '帖子ID（文本）'], [['P1', 'A'], ['P2', 'A']]),
                ('gold_ev.csv', ['样本号', '标签'], [['P1', 'X']]),
            ]:
                self.write_csv(root / name, header, rows)
            with self.assertRaisesRegex(ValueError, '不唯一'):
                cmp.compare(str(root / 'detail.csv'), str(root / 'summary.csv'),
                            str(root / 'gold_ov.csv'), str(root / 'gold_ev.csv'), td)


if __name__ == '__main__':
    unittest.main()
