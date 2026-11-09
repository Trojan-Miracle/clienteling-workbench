import sys
import time
import unittest
from atelier.deadline_provider import run_bounded
from lanxu.provider import ProviderError


class DeadlineTests(unittest.TestCase):
    def test_continuous_output_does_not_extend_total_deadline(self):
        # Reproduces a server keeping the connection active with repeated whitespace.
        script = 'import time,sys\nfor i in range(100):\n print(" ",flush=True); time.sleep(.05)'
        started = time.monotonic()
        with self.assertRaises(ProviderError) as error:
            run_bounded([sys.executable,'-c',script], {}, .25)
        self.assertEqual(error.exception.code, 'total_timeout')
        self.assertLess(time.monotonic()-started, 2)

    def test_success_preserves_actual_model_and_usage(self):
        reply = run_bounded([sys.executable,'-c',
            'print(\'{"text":"ok","metadata":{"model":"actual","usage":{"total_tokens":12}}}\')'], {}, 2)
        self.assertEqual(reply.metadata['model'],'actual')
        self.assertEqual(reply.metadata['usage']['total_tokens'],12)

    def test_configuration_error_remains_nonretryable(self):
        with self.assertRaises(ProviderError) as error:
            run_bounded([sys.executable,'-c',
                'print(\'{"error":{"code":"missing_key","message":"Missing configuration","retryable":false}}\')'], {}, 2)
        self.assertEqual(error.exception.code,'missing_key')
        self.assertFalse(error.exception.retryable)


if __name__ == '__main__':
    unittest.main()
