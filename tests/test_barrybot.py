import unittest
from unittest.mock import Mock, patch

from barrybot import BarryBot, DevNetLLM, DevNetLlmError


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
    def test_provider_usage_and_timing_survive_without_changing_text_return(self):
        response = Mock()
        response.json.return_value = {'model': 'served-model',
            'choices': [{'message': {'content': 'A measured reply.'}, 'finish_reason': 'length'}],
            'usage': {'prompt_tokens': 21, 'completion_tokens': 7, 'total_tokens': 28}}
        llm = DevNetLLM('https://example.invalid', 'provided by runtime', 'requested-model', 'environment', [])
        with patch('barrybot.requests.post', return_value=response):
            with patch('barrybot.time.perf_counter_ns', side_effect=[100, 200, 600, 900]):
                answer = llm.complete([{'role': 'user', 'content': 'Hello'}])
        self.assertEqual(answer, 'A measured reply.')
        self.assertEqual(llm.model, 'requested-model')
        self.assertEqual(llm.last_completion_metadata['model'], 'served-model')
        self.assertEqual(llm.last_completion_metadata['num_total_tokens'], 28)
        self.assertEqual(llm.last_completion_metadata['duration_ns'], 400)
        self.assertEqual(llm.last_completion_metadata['total_duration_ns'], 800)
        self.assertEqual(llm.last_completion_metadata['request_count'], 1)
        self.assertEqual(llm.last_completion_metadata['finish_reason'], 'length')

    def test_served_model_version_does_not_replace_the_proxy_routing_alias(self):
        response = Mock()
        response.json.return_value = {'model': 'served-model-version',
            'choices': [{'message': {'content': 'Hello'}}]}
        llm = DevNetLLM('https://example.invalid', 'provided by runtime', 'routing-alias', 'environment', [])
        with patch('barrybot.requests.post', return_value=response) as request:
            llm.complete([{'role': 'user', 'content': 'First'}])
            llm.complete([{'role': 'user', 'content': 'Second'}])
        self.assertEqual([call.kwargs['json']['model'] for call in request.call_args_list],
            ['routing-alias', 'routing-alias'])

    def test_absent_usage_stays_absent_and_failure_clears_previous_counts(self):
        response = Mock()
        response.json.return_value = {'choices': [{'message': {'content': 'Hello'}}]}
        llm = DevNetLLM('https://example.invalid', 'provided by runtime', 'model', 'environment', [])
        llm.last_completion_metadata = {'num_total_tokens': 999}
        with patch('barrybot.requests.post', return_value=response):
            llm.complete([{'role': 'user', 'content': 'Hello'}])
        self.assertNotIn('num_total_tokens', llm.last_completion_metadata)
        with patch('barrybot.requests.post', side_effect=RuntimeError('unavailable')):
            with patch('barrybot.time.sleep'):
                with self.assertRaises(DevNetLlmError):
                    llm.complete([{'role': 'user', 'content': 'Hello'}])
        self.assertEqual(llm.last_completion_metadata, {})

    def test_invalid_usage_is_not_reported_as_real_token_counts(self):
        response = Mock()
        response.json.return_value = {'choices': [{'message': {'content': 'Hello'}}],
            'usage': {'prompt_tokens': True, 'completion_tokens': -1, 'total_tokens': '25'}}
        llm = DevNetLLM('https://example.invalid', 'provided by runtime', 'model', 'environment', [])
        with patch('barrybot.requests.post', return_value=response):
            llm.complete([{'role': 'user', 'content': 'Hello'}])
        self.assertNotIn('num_total_tokens', llm.last_completion_metadata)
        self.assertNotIn('num_input_tokens', llm.last_completion_metadata)
        self.assertNotIn('num_output_tokens', llm.last_completion_metadata)

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
