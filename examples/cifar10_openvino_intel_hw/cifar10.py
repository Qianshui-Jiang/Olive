# -------------------------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License.
# --------------------------------------------------------------------------
import platform
import tempfile
from pathlib import Path

from olive.workflows import run as olive_run


def main():
    current_path = Path(__file__).absolute().parent
    config_path = current_path / "config.json"
    user_script_path = str(current_path / "user_script.py")

    if platform.system() == "Windows":
        user_script_path = user_script_path.replace("\\", "//")

    with config_path.open() as f:
        file_template_content = f.read()
        file_template_content = file_template_content.replace("{USER_SCRIPT}", user_script_path)

    with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".json", prefix="config_") as f:
        f.write(file_template_content)

    return olive_run(f.name)


if __name__ == "__main__":
    main()
