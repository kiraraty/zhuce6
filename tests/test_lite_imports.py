import importlib
import sys


def test_create_app_lite_mode_does_not_import_full_ops_modules() -> None:
    module_names = [
        "main",
        "dashboard.api",
        "ops.rotate",
        "ops.cleanup",
        "ops.validate",
    ]
    saved_modules = {name: sys.modules.get(name) for name in module_names}
    try:
        for name in module_names:
            sys.modules.pop(name, None)

        main = importlib.import_module("main")
        _app = main.create_app(enable_background_tasks=False, mode="lite")

        assert "ops.rotate" not in sys.modules
        assert "ops.cleanup" not in sys.modules
        assert "ops.validate" not in sys.modules
    finally:
        for name in module_names:
            sys.modules.pop(name, None)
        for name, module in saved_modules.items():
            if module is not None:
                sys.modules[name] = module
