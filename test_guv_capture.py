"""GUV dictation preserves original statements and separates pending/corrected data."""
import unittest

from guv_capture import extract_fields, load_template, missing_question, summary_lines, template_text


class CaptureTest(unittest.TestCase):
    def setUp(self):
        self.template = load_template()

    def test_multiturn_capture_and_unconfirmed_file_link(self):
        notes = ["A样品拍了3个视野，看到少量聚集。", "原图叫 A01.tif"]
        fields = extract_fields(self.template, notes)
        self.assertIn((1, "A样品拍了3个视野"), fields["sample"])
        self.assertIn((1, "A样品拍了3个视野"), fields["location"])
        self.assertIn((1, "看到少量聚集"), fields["observation"])
        self.assertEqual(fields["files"], [(2, "原图叫 A01.tif")])
        self.assertEqual(fields["linking"], [])
        self.assertIn("A01.tif", missing_question(self.template, fields))
        self.assertEqual(missing_question(self.template, fields).count("？"), 1)
        fields = extract_fields(self.template, notes + ["样品A的视野1对应A01.tif", "原图叫 B02.tif"])
        self.assertIn("B02.tif", missing_question(self.template, fields))

    def test_plan_uncertainty_and_correction_preserve_voice(self):
        fields = extract_fields(self.template, ["计划在 A1 孔拍3个视野，原图叫 future.tif。",
                                              "A样品拍了2个视野，看到一个圆形结构，可能是气泡。",
                                              "原图回工位再补。更正：A样品实际拍了1个视野。",
                                              "实测：0.50 µm；记录人：甲；实际操作者：乙"])
        self.assertFalse(fields["files"])
        self.assertEqual(len(fields["plan"]), 2)
        self.assertEqual(fields["pending"], [(3, "原图回工位再补")])
        self.assertEqual(fields["interpretation"], [(2, "可能是气泡")])
        self.assertEqual(fields["correction"], [(3, "更正：A样品实际拍了1个视野")])
        self.assertEqual(fields["location"], [(2, "A样品拍了2个视野")])
        self.assertEqual(fields["operator"], [(4, "实际操作者：乙")])
        self.assertIn((4, "实测：0.50 µm"), fields["observation"])
        self.assertNotIn("文件", missing_question(self.template, fields))

    def test_questions_and_past_reports_do_not_become_current_results(self):
        fields = extract_fields(self.template, ["A样品拍了3个视野吗？上批 A样品拍了2个视野，这次 A样品拍了1个视野。"])
        self.assertEqual(fields["pending"], [(1, "A样品拍了3个视野吗？")])
        self.assertEqual(fields["background"], [(1, "上批 A样品拍了2个视野")])
        self.assertEqual(fields["location"], [(1, "这次 A样品拍了1个视野")])

    def test_sample_demo_groups_sources_and_counts(self):
        notes = ["样品：DEMO-A，批次 DEMO-B01。目的：比较同一样品在孔中心与边缘的可见囊泡。",
                 "计划：拍两个固定视野。实际：在 A1 孔中心拍了 F01，明场，20× 物镜。观察：F01 可辨囊泡 6 个。边缘还没拍。",
                 "原始文件：DEMO-A_F01.tif 对应样品 DEMO-A、A1 孔中心 F01，仅登记文件名。更正：复看后 F01 应为 5 个，先前重复数了一个。"]
        fields = extract_fields(self.template, notes)
        self.assertIn((1, "批次 DEMO-B01"), fields["sample"])
        self.assertEqual(fields["plan"], [(2, "计划：拍两个固定视野")])
        self.assertIn((2, "实际：在 A1 孔中心拍了 F01"), fields["location"])
        self.assertIn((2, "20× 物镜"), fields["acquisition"])
        self.assertIn((2, "观察：F01 可辨囊泡 6 个"), fields["observation"])
        self.assertIn((3, "更正：复看后 F01 应为 5 个"), fields["correction"])
        self.assertTrue(any("DEMO-A_F01.tif" in s for _,s in fields["linking"]))
        self.assertEqual(fields["pending"], [(2, "边缘还没拍")])
        self.assertFalse(fields["operator"])
        self.assertFalse(missing_question(self.template, fields))
        self.assertIn("〔第2条〕明场", summary_lines(self.template, fields))
        self.assertIn("v0.1", template_text())
