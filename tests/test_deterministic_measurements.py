"""Dense measurement inputs keep their lexical features without quadratic work."""
import sys

from engraphis.backends.embedder_deterministic import _variant_features


def test_measurement_window_preserves_order_and_first_rate_unit():
    tokens = ["60", "seconds", "and", "1", "minute"]
    assert _variant_features("60 seconds and 1/minute", tokens) == [
        "measure:second:60", "measure:second:1", "measure:second:60",
        "rate:second:60", "rate:second:1",
    ]


def test_dense_measurements_require_linear_python_work():
    counts = []
    for repetitions in (100, 200):
        steps = [0]

        def trace(frame, event, arg):
            if frame.f_code is _variant_features.__code__ and event == "line":
                steps[0] += 1
            return trace

        previous_trace = sys.gettrace()
        try:
            sys.settrace(trace)
            _variant_features("1 second " * repetitions, ["1", "second"] * repetitions)
        finally:
            sys.settrace(previous_trace)
        counts.append(steps[0])
    # Count work, not wall-clock time, so slow/loaded CI machines remain stable.
    assert counts[1] < counts[0] * 2.2
