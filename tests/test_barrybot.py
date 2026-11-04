import unittest
from unittest.mock import Mock

from barrybot import BarryBot, DevNetLlmError


class BrokenLLM:
    model = "unavailable-model"
    model_source = "devnet-models"

    def complete(self, messages, max_tokens=96):
        raise DevNetLlmError("model service unavailable")


class WorkingLLM:
    model = "working-model"
    model_source = "devnet-models"

    def complete(self, messages, max_tokens=96):
        return "The policy requires a verified account."


class BarryBotTests(unittest.TestCase):
    def test_empty_completion_is_not_recorded_as_a_live_response(self):
        for content in (None, "", "   ", {}):
            with self.subTest(content=content):
                llm = WorkingLLM()
                llm.complete = Mock(return_value=content)
                bot = BarryBot(llm)
                with self.assertRaisesRegex(DevNetLlmError, "empty"):
                    bot.ask("Confirm the model is ready.", allow_fallback=False)
                self.assertEqual(bot.last_response_metadata["response_source"], "unavailable")

    def test_canned_fallback_has_explicit_response_source(self):
        bot = BarryBot(BrokenLLM())
        answer = bot.ask("What should I evaluate?")

        self.assertEqual(answer, BarryBot.fallback_answer("What should I evaluate?"))
        metadata = getattr(bot, "last_response_metadata", {})
        self.assertEqual(metadata.get("response_source"), "fallback")
        self.assertTrue(metadata.get("used_fallback"))
        self.assertIsNone(metadata.get("model"))

    def test_health_check_can_require_a_live_model(self):
        bot = BarryBot(BrokenLLM())
        with self.assertRaises(DevNetLlmError):
            bot.ask("Confirm the model is ready.", allow_fallback=False)

    def test_successful_response_records_the_model(self):
        bot = BarryBot(WorkingLLM())
        self.assertEqual(bot.ask("Explain the policy."), "The policy requires a verified account.")
        metadata = getattr(bot, "last_response_metadata", {})
        self.assertEqual(metadata.get("response_source"), "llm")
        self.assertFalse(metadata.get("used_fallback"))
        self.assertEqual(metadata.get("model"), "working-model")


if __name__ == "__main__":
    unittest.main()
