import runpy
import pathlib

def test_regression_script():
    # Run the legacy regression script. It uses offscreen Qt and asserts internally.
    root = pathlib.Path(__file__).resolve().parents[1]
    script = root / "_regression.py"
    assert script.exists(), "_regression.py missing"
    runpy.run_path(str(script), run_name="__main__")
