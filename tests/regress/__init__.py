import os


def load_tests(loader, standard_tests, pattern):
    here = os.path.dirname(os.path.abspath(__file__))
    repo_root = os.path.dirname(os.path.dirname(here))
    standard_tests.addTests(
        loader.discover(here, pattern="regress_*.py", top_level_dir=repo_root)
    )
    return standard_tests
