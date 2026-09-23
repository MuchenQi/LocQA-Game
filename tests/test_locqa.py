"""Test suite. Run: python -m unittest discover -s tests -v"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from locqa import Config, Script, Segment, Severity, load, run_checks, select  # noqa: E402
from locqa.checks.graph import _ending_shape  # noqa: E402
from locqa.checks.integrity import extract_placeholders, extract_tags  # noqa: E402
from locqa.checks.voice import classify_register  # noqa: E402
from locqa.model import Node, display_width, wrap_by_width  # noqa: E402
from locqa.parsers.sheet import resolve_columns  # noqa: E402
from locqa.pseudo import pseudolocalize  # noqa: E402

SAMPLES = os.path.join(os.path.dirname(__file__), "..", "samples")


def build(pairs, **kwargs) -> Script:
    s = Script(name="test")
    for i, (src, tgt) in enumerate(pairs):
        s.add_segment(Segment(id=f"s{i}", source=src, target=tgt, **kwargs))
    return s


def ids(findings, check_id):
    return [f.segment_id for f in findings if f.check_id == check_id]


def run(script, check_ids, config=None):
    return run_checks(script, config or Config(), select(only=check_ids))


class TestWidth(unittest.TestCase):
    def test_halfwidth_units(self):
        self.assertEqual(display_width("abc"), 3)
        self.assertEqual(display_width("こんにちは"), 10)
        self.assertEqual(display_width("HP: 100"), 7)
        self.assertEqual(display_width("ｱｲｳ"), 3)          # half-width katakana

    def test_ambiguous_is_wide_for_japanese(self):
        self.assertEqual(display_width("±", ambiguous_wide=True), 2)
        self.assertEqual(display_width("±", ambiguous_wide=False), 1)

    def test_wrap_breaks_per_character(self):
        self.assertEqual(wrap_by_width("あいうえお", 4), ["あい", "うえ", "お"])
        self.assertEqual(wrap_by_width("abcdef", 3), ["abc", "def"])

    def test_wrap_respects_explicit_newlines(self):
        self.assertEqual(wrap_by_width("ab\ncd", 10), ["ab", "cd"])


class TestExtraction(unittest.TestCase):
    def test_placeholder_forms(self):
        self.assertEqual(extract_placeholders("Hi {NAME}, you have %d gold"), ["{NAME}", "%d"])
        self.assertEqual(extract_placeholders("%1$s beat %2$s"), ["%1$s", "%2$s"])
        self.assertEqual(extract_placeholders("Press [BUTTON_A]"), ["[BUTTON_A]"])
        self.assertEqual(extract_placeholders("Hello <<player>>"), ["<<player>>"])

    def test_no_overlap_between_patterns(self):
        # %1$s must be one token, not %1 plus stray text.
        self.assertEqual(extract_placeholders("%1$s"), ["%1$s"])

    def test_tags(self):
        self.assertEqual(extract_tags("<b>hi</b>"), ["<b>", "</b>"])
        self.assertIn("[color=red]", extract_tags("[color=red]hi[/color]"))


class TestIntegrityChecks(unittest.TestCase):
    def test_missing_placeholder_is_blocker(self):
        s = build([("You found {ITEM}!", "見つけた！")])
        f = run(s, ["placeholder.parity"])
        self.assertEqual(len(f), 1)
        self.assertIs(f[0].severity, Severity.BLOCKER)

    def test_matching_placeholders_pass(self):
        s = build([("You found {ITEM}!", "{ITEM}を見つけた！")])
        self.assertEqual(run(s, ["placeholder.parity"]), [])

    def test_fullwidth_brace_detected(self):
        s = build([("Got {ITEM}", "｛ITEM｝を入手")])
        self.assertTrue(run(s, ["placeholder.corrupt"]))

    def test_fullwidth_percent_is_not_a_false_positive(self):
        # The source % is literal text, not a printf token, so ％ is correct.
        s = build([("Crit rate {PCT}% up", "クリティカル率{PCT}％上昇")])
        self.assertEqual(run(s, ["placeholder.corrupt"]), [])

    def test_unclosed_tag(self):
        s = build([("<b>Warning</b>", "<b>警告")])
        f = run(s, ["tag.parity"])
        self.assertTrue(any("never closed" in x.message for x in f))

    def test_crossed_tags(self):
        s = build([("<b><i>x</i></b>", "<b><i>あ</b></i>")])
        self.assertTrue(any("crosses" in x.message for x in run(s, ["tag.parity"])))


class TestJapaneseChecks(unittest.TestCase):
    def test_charset_flags_platform_dependent_characters(self):
        s = build([("First Steps", "はじめの一歩①")])
        f = run(s, ["ja.charset"], Config(charset="shift_jis"))
        self.assertEqual(len(f), 1)
        self.assertIn("①", f[0].meta["characters"])

    def test_charset_passes_in_repertoire_text(self):
        s = build([("Hello", "こんにちは")])
        self.assertEqual(run(s, ["ja.charset"], Config(charset="shift_jis")), [])

    def test_source_copied_target(self):
        s = build([("Hard", "Hard")])
        f = run(s, ["ja.untranslated"])
        self.assertIs(f[0].severity, Severity.BLOCKER)

    def test_dnt_segment_is_not_flagged_as_untranslated(self):
        s = Script(name="t")
        s.add_segment(Segment(id="a", source="Aetherbound", target="Aetherbound", do_not_translate=True))
        self.assertEqual(run(s, ["ja.untranslated"]), [])

    def test_halfwidth_period_in_japanese(self):
        s = build([("Save data is full.", "セーブデータがいっぱいです.")])
        self.assertTrue(run(s, ["ja.punctuation"]))

    def test_punctuation_ignores_embedded_english(self):
        s = build([("Version 1.2 available", "バージョン 1.2 が利用可能です。")])
        f = run(s, ["ja.punctuation"])
        self.assertEqual([x for x in f if "Half-width ." in x.message], [])

    def test_kinsoku_line_head(self):
        # Budget 24 = 12 full-width cells; the 。 is pushed to line 2.
        s = build([("x", "スロットにセーブしました。")], max_width=24)
        f = run(s, ["ja.kinsoku"])
        self.assertTrue(any("行頭禁則" in x.message for x in f))

    def test_wave_dash_only_fires_on_mixed_usage(self):
        consistent = build([("a", "またね〜"), ("b", "いくよ〜")])
        self.assertEqual(run(consistent, ["ja.wave_dash"]), [])
        mixed = build([("a", "またね〜"), ("b", "いくよ～")])
        self.assertEqual(len(run(mixed, ["ja.wave_dash"])), 2)

    def test_mojibake(self):
        s = build([("Hello", "ã\x81\x93ã\x82\x93")])
        self.assertTrue(run(s, ["ja.mojibake"]))


class TestLength(unittest.TestCase):
    def test_overflow_reported_once(self):
        # Width overflow implies the line budget is blown; report only the cause.
        s = build([("x", "デスクトップに終了する")], max_width=16, max_lines=1)
        f = run(s, ["length.budget"])
        self.assertEqual(len(f), 1)
        self.assertIn("half-width units", f[0].message)

    def test_placeholder_expanded_to_worst_case(self):
        cfg = Config(placeholder_widths={"{ITEM}": 14})
        s = build([("Found {ITEM}", "{ITEM}を入手")], max_width=16)
        self.assertTrue(run(s, ["length.budget"], cfg))

    def test_within_budget_passes(self):
        s = build([("Start", "はじめる")], max_width=16, max_lines=1)
        self.assertEqual(run(s, ["length.budget"]), [])


class TestTerminology(unittest.TestCase):
    def setUp(self):
        self.cfg = Config(
            glossary={"Save Point": ["セーブポイント"]},
            forbidden={"保存地点": "use the katakana form"},
            do_not_translate=["Aetherbound"],
        )

    def test_glossary_violation(self):
        s = build([("Save at this Save Point?", "この保存地点でセーブしますか？")])
        self.assertTrue(run(s, ["term.glossary"], self.cfg))

    def test_glossary_satisfied(self):
        s = build([("Save at this Save Point?", "このセーブポイントでセーブしますか？")])
        self.assertEqual(run(s, ["term.glossary"], self.cfg), [])

    def test_forbidden_term(self):
        s = build([("x", "保存地点")])
        self.assertTrue(run(s, ["term.forbidden"], self.cfg))

    def test_dnt_dropped(self):
        s = build([("The Aetherbound Compact", "エーテルバウンドの盟約")])
        self.assertTrue(run(s, ["term.dnt"], self.cfg))

    def test_inconsistent_translation(self):
        s = build([("Cancel", "キャンセル"), ("Cancel", "もどる")])
        self.assertEqual(len(run(s, ["consistency.source"])), 2)


class TestRegisterClassifier(unittest.TestCase):
    def test_polite(self):
        for t in ["行きます。", "そうですね。", "お許しください。", "居られません。"]:
            self.assertEqual(classify_register(t), "POLITE", t)

    def test_plain(self):
        for t in ["沈黙にも礼儀がある。", "行くぞ。", "ただの扉だ。", "好きじゃない。"]:
            self.assertEqual(classify_register(t), "PLAIN", t)

    def test_neutral_fragments_are_not_guessed(self):
        # 体言止め and interjections carry no register; counting them is the
        # main source of noise in a naive implementation.
        for t in ["そうか。", "扉。", "……", "エーテル門"]:
            self.assertEqual(classify_register(t), "NEUTRAL", t)

    def test_mixed(self):
        self.assertEqual(classify_register("行きます。だが遅い。"), "MIXED")

    def test_masu_not_misread_as_plain_su_verb(self):
        self.assertEqual(classify_register("使用します。"), "POLITE")


class TestVoiceChecks(unittest.TestCase):
    def _speaker_script(self, targets, speaker="Kestrel"):
        s = Script(name="t")
        for i, t in enumerate(targets):
            s.add_segment(Segment(id=f"s{i}", source="x", target=t, speaker=speaker))
        return s

    def test_first_person_drift(self):
        s = self._speaker_script(["俺は行く。", "俺の話だ。", "僕たちは知られている。"])
        f = run(s, ["voice.first_person"])
        self.assertEqual(len(f), 1)
        self.assertEqual(f[0].meta["found"], "僕")

    def test_consistent_pronoun_passes(self):
        s = self._speaker_script(["俺は行く。", "俺の話だ。", "俺は待たない。"])
        self.assertEqual(run(s, ["voice.first_person"]), [])

    def test_register_drift(self):
        s = self._speaker_script(
            ["行きます。", "そうですね。", "お待ちください。", "参ります。", "沈黙にも礼儀がある。"],
            speaker="Aldis",
        )
        f = run(s, ["voice.register"])
        self.assertEqual(len(f), 1)
        self.assertEqual(f[0].meta["found"], "PLAIN")

    def test_genuinely_mixed_speaker_is_left_alone(self):
        s = self._speaker_script(["行きます。", "そうですね。", "行くぞ。", "扉だ。"])
        self.assertEqual(run(s, ["voice.register"]), [])

    def test_too_few_lines_to_judge(self):
        s = self._speaker_script(["行きます。", "行くぞ。"])
        self.assertEqual(run(s, ["voice.register"]), [])


class TestGraphModel(unittest.TestCase):
    def setUp(self):
        self.s = Script(name="t")
        for nid, succ in [("a", ["b", "c"]), ("b", ["d"]), ("c", ["d"]), ("d", []), ("orphan", [])]:
            self.s.add_node(Node(id=nid, successors=succ))
        self.s.entry_nodes = ["a"]

    def test_depths(self):
        self.assertEqual(self.s.depths(), {"a": 0, "b": 1, "c": 1, "d": 2})

    def test_unreachable(self):
        self.assertEqual(self.s.unreachable_nodes(), ["orphan"])

    def test_predecessors(self):
        self.assertEqual(sorted(self.s.predecessors()["d"]), ["b", "c"])

    def test_reach_decays_with_depth(self):
        for nid in ("a", "d"):
            self.s.add_segment(Segment(id=f"seg_{nid}", source="Cancel", target="Hard", node_id=nid))
        f = run_checks(self.s, Config(), select(only=["ja.untranslated"]))
        reach = {x.node_id: x.reach for x in f}
        self.assertGreater(reach["a"], reach["d"])


class TestGraphChecks(unittest.TestCase):
    def test_ending_shape(self):
        self.assertEqual(_ending_shape("門とは何かを尋ねます"), "ますform")
        self.assertEqual(_ending_shape("遅れたことを謝る"), "plain verb")
        self.assertEqual(_ending_shape("何も言わない"), "plain verb")   # ない is verbal
        self.assertEqual(_ending_shape("門をくぐる"), "plain verb")

    def test_choice_parallelism_flags_the_outlier_only(self):
        s = Script(name="t")
        s.add_node(Node(id="m", kind="menu", successors=["x"]))
        s.add_node(Node(id="x"))
        for i, t in enumerate(["謝る", "尋ねます", "言わない"]):
            s.add_segment(Segment(id=f"c{i}", source="x", target=t, node_id="m", is_choice=True))
        f = run(s, ["graph.choice_parallelism"])
        self.assertTrue(all(x.segment_id == "c1" for x in f), [x.segment_id for x in f])

    def test_anaphora_only_at_a_merge(self):
        s = Script(name="t")
        s.add_node(Node(id="p1", successors=["m"]))
        s.add_node(Node(id="p2", successors=["m"]))
        s.add_node(Node(id="m"))
        s.add_segment(Segment(id="a", source="x", target="そうか。分かった。", node_id="m"))
        self.assertEqual(len(run(s, ["graph.anaphora"])), 1)

        single = Script(name="t2")
        single.add_node(Node(id="p1", successors=["m"]))
        single.add_node(Node(id="m"))
        single.add_segment(Segment(id="a", source="x", target="そうか。分かった。", node_id="m"))
        self.assertEqual(run(single, ["graph.anaphora"]), [])

    def test_dangling_edge(self):
        s = Script(name="t")
        s.add_node(Node(id="a", successors=["nowhere"]))
        self.assertTrue(run(s, ["graph.dangling"]))


class TestSheetParser(unittest.TestCase):
    def test_column_synonyms(self):
        cols = resolve_columns(["key", "english", "japanese", "max_width", "character"])
        self.assertEqual(cols["id"], "key")
        self.assertEqual(cols["source"], "english")
        self.assertEqual(cols["target"], "japanese")
        self.assertEqual(cols["speaker"], "character")

    def test_alternate_naming(self):
        cols = resolve_columns(["StringID", "Source Text", "JP", "Char Limit"])
        self.assertEqual(cols["id"], "StringID")
        self.assertEqual(cols["target"], "JP")

    def test_loads_sample(self):
        s = load(os.path.join(SAMPLES, "aetherbound_ui.csv"))
        self.assertEqual(len(s), 27)
        self.assertEqual(s.segments["UI_BTN_CANCEL"].max_width, 8)


class TestXliffParser(unittest.TestCase):
    def setUp(self):
        self.s = load(os.path.join(SAMPLES, "aetherbound_ch1.xlf"))

    def test_units_loaded(self):
        self.assertEqual(len(self.s), 6)
        self.assertEqual(self.s.source_lang, "en")
        self.assertEqual(self.s.target_lang, "ja")

    def test_ph_equiv_becomes_visible_placeholder(self):
        seg = self.s.segments["TUTORIAL_ATTACK"]
        self.assertIn("{BTN_A}", seg.source)
        self.assertIn("{BTN_A}", seg.target)

    def test_dropped_placeholder_is_caught_through_xliff(self):
        f = run(self.s, ["placeholder.parity"])
        self.assertEqual(ids(f, "placeholder.parity"), ["TUTORIAL_GUARD"])

    def test_structured_notes_parsed(self):
        seg = self.s.segments["DLG_KESTREL_01"]
        self.assertEqual(seg.speaker, "Kestrel")
        self.assertEqual(seg.node_id, "tutorial_end")
        self.assertEqual(seg.max_width, 30)

    def test_translate_no_marks_dnt(self):
        self.assertTrue(self.s.segments["SYS_AUTOSAVE"].do_not_translate)


class TestRenpyParser(unittest.TestCase):
    def setUp(self):
        self.s = load(
            os.path.join(SAMPLES, "aetherbound.rpy"),
            translations=[os.path.join(SAMPLES, "aetherbound_ja.rpy")],
        )

    def test_graph_recovered(self):
        self.assertTrue(self.s.has_graph)
        self.assertEqual(self.s.entry_nodes, ["start"])
        self.assertIn("start:menu1", self.s.nodes["start"].successors)

    def test_branches_converge(self):
        self.assertEqual(len(self.s.predecessors()["gate_open"]), 3)

    def test_character_names_resolved(self):
        speakers = {s.speaker for s in self.s if s.speaker}
        self.assertIn("Sister Aldis", speakers)
        self.assertIn("Kestrel", speakers)

    def test_all_lines_translated_from_comment_join(self):
        self.assertEqual([s.id for s in self.s if not s.target], [])

    def test_cut_content_is_unreachable(self):
        self.assertEqual(self.s.unreachable_nodes(), ["cut_shrine_scene"])

    def test_fallthrough_not_invented_after_return(self):
        # chapter_two ends in `return`, so it must not fall through.
        self.assertEqual(self.s.nodes["chapter_two"].successors, [])


class TestPseudo(unittest.TestCase):
    def test_placeholders_survive(self):
        out = pseudolocalize("Press {BTN_A} to attack")
        self.assertIn("{BTN_A}", out)

    def test_expands_and_brackets(self):
        out = pseudolocalize("Start", expansion=2.0)
        self.assertTrue(out.startswith("⟦") and out.endswith("⟧"))
        self.assertGreater(len(out), len("Start") + 2)

    def test_tags_survive(self):
        self.assertIn("<b>", pseudolocalize("<b>Warning</b>"))


class TestEndToEnd(unittest.TestCase):
    def test_sample_project_findings(self):
        cfg = Config.load(os.path.join(SAMPLES, "aetherbound.locqa.yml"))
        s = load(os.path.join(SAMPLES, "aetherbound_ui.csv"))
        f = run_checks(s, cfg)
        found = {x.check_id for x in f}
        for expected in ["placeholder.parity", "ja.charset", "term.glossary",
                         "term.dnt", "consistency.source", "ja.wave_dash"]:
            self.assertIn(expected, found, f"{expected} did not fire on the sample")

    def test_findings_sorted_by_priority(self):
        cfg = Config.load(os.path.join(SAMPLES, "aetherbound.locqa.yml"))
        s = load(os.path.join(SAMPLES, "aetherbound.rpy"),
                 translations=[os.path.join(SAMPLES, "aetherbound_ja.rpy")])
        f = run_checks(s, cfg)
        self.assertEqual([x.priority for x in f], sorted((x.priority for x in f), reverse=True))

    def test_reporters_all_render(self):
        from locqa import report
        from locqa.htmlreport import to_html

        cfg = Config.load(os.path.join(SAMPLES, "aetherbound.locqa.yml"))
        s = load(os.path.join(SAMPLES, "aetherbound_ui.csv"))
        f = run_checks(s, cfg)
        self.assertIn("aetherbound", report.console(s, f, color=False))
        self.assertIn('"findings"', report.to_json(s, f))
        self.assertIn("<testsuites", report.to_junit(s, f))
        self.assertIn("severity", report.to_csv(s, f))
        self.assertIn("<!doctype html>", to_html(s, f, cfg))


if __name__ == "__main__":
    unittest.main(verbosity=2)
