"""Regression coverage for the documented offline grounded-recall gate."""

from eval import grounded


def test_grounded_eval_report_is_windows_console_safe(capsys):
    """The release command must work with Windows' default cp1252 stdout."""
    grounded.main()
    capsys.readouterr().out.encode("cp1252")
