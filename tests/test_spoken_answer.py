"""Unit tests for the read-in-full or summarise rule.

Run from the project root: python -m unittest tests.test_spoken_answer
"""

import unittest

from plugins import _spoken_answer as spoken


class SpokenAnswerTests(unittest.TestCase):
    def test_up_to_two_thousand_words_is_read_in_full(self):
        text = " ".join(["palabra"] * 2_000)
        self.assertEqual(spoken.spoken_answer(text), "[READ_IN_FULL]\n" + text)

    def test_past_two_thousand_words_is_summarised(self):
        text = " ".join(["palabra"] * 2_001)
        self.assertEqual(spoken.spoken_answer(text), "[ANSWER_IN_DEPTH]\n" + text)

    def test_a_runaway_answer_is_capped_but_still_summarised(self):
        result = spoken.spoken_answer("palabra " * 20_000)
        self.assertTrue(result.startswith("[ANSWER_IN_DEPTH]\n"))
        self.assertLessEqual(len(result), len("[ANSWER_IN_DEPTH]\n") + spoken.MAX_CHARS + 1)


if __name__ == "__main__":
    unittest.main()
