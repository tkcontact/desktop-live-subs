import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sentences import CaptionTracker, FragmentMerger, RecentFilter, Unit, VoiceRef, pick_ref  # noqa: E402


class CaptionTrackerTest(unittest.TestCase):
    def feed(self, tracker, steps):
        """[(時刻, テキスト), ...] を順に渡し、出てきたものを返す"""
        out = []
        for now, text in steps:
            got = tracker.update(text, now)
            if got:
                out.append(got)
        return out

    def test_existing_text_is_skipped(self):
        t = CaptionTracker()
        self.assertEqual(self.feed(t, [(0, "Old sentence. Another old one."), (5, "Old sentence. Another old one.")]), [])

    def test_sentence_confirmed_when_followed_by_more(self):
        t = CaptionTracker()
        out = self.feed(t, [
            (0.0, ""),
            (0.1, "So today we"),
            (0.2, "So today we are going to talk"),
            (0.3, "So today we are going to talk. About"),
        ])
        self.assertEqual(out, ["So today we are going to talk."])

    def test_trailing_period_confirmed_after_stable(self):
        t = CaptionTracker(stable_sec=0.4)
        out = self.feed(t, [(0.0, ""), (0.1, "That's it."), (0.3, "That's it."), (0.6, "That's it.")])
        self.assertEqual(out, ["That's it."])

    def test_revision_of_emitted_text_is_not_reemitted(self):
        t = CaptionTracker()
        out = self.feed(t, [
            (0.0, ""),
            (0.1, "So today we are going to talk. About"),
            (0.2, "So today we're going to talk. About the"),
            (0.3, "So today, we're going to talk. About the weather. It"),
        ])
        self.assertEqual(out, ["So today we are going to talk.", "About the weather."])

    def test_scroll_keeps_position(self):
        t = CaptionTracker()
        out = self.feed(t, [
            (0.0, ""),
            (0.1, "First one here. Second one here. Third"),
            (0.2, "here. Second one here. Third one here. Fourth"),
        ])
        self.assertEqual(out, ["First one here. Second one here.", "Third one here."])

    def test_idle_flush_then_continuation(self):
        t = CaptionTracker(idle_sec=1.2)
        out = self.feed(t, [
            (0.0, ""),
            (0.1, "And then we"),
            (1.0, "And then we"),
            (1.4, "And then we"),
            (2.0, "And then we left the house. OK"),
        ])
        self.assertEqual(out, ["And then we", "left the house."])

    def test_repeated_sentences_are_each_emitted(self):
        t = CaptionTracker()
        out = self.feed(t, [(0.0, ""), (0.1, "Go. Go"), (0.2, "Go. Go. Go"), (0.3, "Go. Go. Go. Yes")])
        self.assertEqual(out, ["Go.", "Go.", "Go."])

    def test_newline_confirms_line(self):
        t = CaptionTracker()
        out = self.feed(t, [(0.0, ""), (0.1, "we will see\nnext"), (0.2, "we will see\nnext time")])
        self.assertEqual(out, ["we will see"])

    def test_cleared_text_starts_fresh(self):
        t = CaptionTracker()
        out = self.feed(t, [
            (0.0, ""),
            (0.1, "Hello there. How"),
            (0.2, "Brand new text here. Next"),
        ])
        self.assertEqual(out, ["Hello there.", "Brand new text here."])


class FragmentMergerTest(unittest.TestCase):
    def test_long_sentence_passes(self):
        m = FragmentMerger()
        self.assertEqual([u.text for u in m.add(Unit("This is a long sentence that ends here.", 0), 0)],
                         ["This is a long sentence that ends here."])

    def test_short_fragment_merged_with_next(self):
        m = FragmentMerger()
        self.assertEqual(m.add(Unit("Yeah.", 0), 0), [])
        out = m.add(Unit("I think that's right.", 1), 1)
        self.assertEqual([(u.text, u.parts) for u in out], [("Yeah. I think that's right.", 2)])

    def test_short_fragment_flushed_after_silence(self):
        m = FragmentMerger(hold_sec=1.2)
        m.add(Unit("Okay.", 0), 0)
        self.assertEqual(m.flush_due(1.0, speaking=False), [])
        self.assertEqual(m.flush_due(1.0, speaking=True), [])
        self.assertEqual([u.text for u in m.flush_due(1.3, speaking=False)], ["Okay."])

    def test_short_sentence_waits_only_briefly_while_speaking(self):
        m = FragmentMerger(short_max_sec=2.5)
        m.add(Unit("It's the school.", 0), 0)
        self.assertEqual(m.flush_due(2.4, speaking=True), [])
        self.assertEqual([u.text for u in m.flush_due(2.5, speaking=True)], ["It's the school."])

    def test_unfinished_waits_for_next_chunk_while_speaking(self):
        m = FragmentMerger(tail_max_sec=9)
        m.add(Unit("and then", 0), 0)
        self.assertEqual(m.flush_due(8.9, speaking=True), [])
        self.assertEqual([u.text for u in m.flush_due(9.0, speaking=True)], ["and then"])

    def test_short_tail_waits_for_next(self):
        m = FragmentMerger()
        out = m.add(Unit("I went to the store yesterday. And then", 0, audio_sec=3), 0)
        self.assertEqual([u.text for u in out], ["I went to the store yesterday."])
        out = m.add(Unit("I saw him there.", 1), 1)
        self.assertEqual([u.text for u in out], ["And then I saw him there."])

    def test_cut_chunk_holds_last_sentence_even_with_period(self):
        m = FragmentMerger()
        out = m.add(Unit("Why are we the way we are? I had a hypothesis that our brains were actually different.", 0, cut=True), 0)
        self.assertEqual([u.text for u in out], ["Why are we the way we are?"])
        out = m.add(Unit("than the brains of other people.", 8), 8)
        self.assertEqual([u.text for u in out],
                         ["I had a hypothesis that our brains were actually different. than the brains of other people."])

    def test_unfinished_without_sentence_end_splits_at_comma(self):
        m = FragmentMerger()
        out = m.add(Unit("My behaviors always perplexed the people around me, and I wanted to explain to the world", 0, cut=True), 0)
        self.assertEqual([u.text for u in out], ["My behaviors always perplexed the people around me,"])
        self.assertEqual(m.pending.text, "and I wanted to explain to the world")

    def test_long_unbroken_text_is_not_held(self):
        m = FragmentMerger()
        text = " ".join(["word"] * 30)
        self.assertEqual([u.text for u in m.add(Unit(text, 0, cut=True), 0)], [text])


class PickRefTest(unittest.TestCase):
    def ref(self, sec):
        return VoiceRef([0.0] * int(sec * 10), 10, f"{sec}s")

    def test_newer_unless_too_short(self):
        self.assertEqual(pick_ref(self.ref(3), self.ref(2.5)).text, "2.5s")
        self.assertEqual(pick_ref(self.ref(3), self.ref(1)).text, "3s")
        self.assertEqual(pick_ref(self.ref(0.5), self.ref(1)).text, "1s")
        self.assertEqual(pick_ref(None, self.ref(1)).text, "1s")
        self.assertEqual(pick_ref(self.ref(1), None).text, "1s")

    def test_merger_keeps_ref(self):
        m = FragmentMerger()
        m.add(Unit("Yeah.", 0, ref=self.ref(3)), 0)
        out = m.add(Unit("I think that's right.", 1, ref=self.ref(1)), 1)
        self.assertEqual(out[0].ref.text, "3s")


class RecentFilterTest(unittest.TestCase):
    def test_duplicates(self):
        f = RecentFilter(window_sec=30)
        self.assertFalse(f.seen("Let's go to the next area.", 0))
        self.assertTrue(f.seen("let's go to the next area", 5))
        self.assertTrue(f.seen("Lets go to the next area.", 6))
        self.assertFalse(f.seen("Let's go to the next area.", 60))
        self.assertTrue(f.seen("...", 61))


if __name__ == "__main__":
    unittest.main()
