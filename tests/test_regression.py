import pathlib
import runpy


def test_regression_script():
    # Run the legacy regression script. It uses offscreen Qt and asserts internally.
    root = pathlib.Path(__file__).resolve().parents[1]
    script = root / "_regression.py"
    assert script.exists(), "_regression.py missing"
    from ui import dialogs

    orig_completion = dialogs.completion
    orig_inform = dialogs.inform
    try:
        runpy.run_path(str(script), run_name="__main__")
    finally:
        dialogs.completion = orig_completion
        dialogs.inform = orig_inform
