import importlib


def test_packages_importable():
    for pkg in ("clients", "handlers", "services", "models"):
        importlib.import_module(pkg)
