"""Install the offline English-to-Russian Argos Translate model."""

import os
from pathlib import Path


ARGOS_DATA_DIR = Path(__file__).parent / ".argos"
os.environ.setdefault("XDG_DATA_HOME", str(ARGOS_DATA_DIR / "data"))
os.environ.setdefault("XDG_CONFIG_HOME", str(ARGOS_DATA_DIR / "config"))
os.environ.setdefault("XDG_CACHE_HOME", str(ARGOS_DATA_DIR / "cache"))
os.environ.setdefault("ARGOS_PACKAGES_DIR", str(ARGOS_DATA_DIR / "packages"))

import argostranslate.package
import argostranslate.translate


SOURCE_LANGUAGE = "en"
TARGET_LANGUAGE = "ru"


def model_is_installed() -> bool:
    languages = {
        language.code: language
        for language in argostranslate.translate.get_installed_languages()
    }
    source = languages.get(SOURCE_LANGUAGE)
    target = languages.get(TARGET_LANGUAGE)
    if source is None or target is None:
        return False
    try:
        source.get_translation(target)
    except Exception:
        return False
    return True


def main() -> None:
    if model_is_installed():
        print("Argos Translate en-ru model is already installed.")
        return

    print("Downloading the Argos Translate en-ru model...")
    argostranslate.package.update_package_index()
    package = next(
        (
            item
            for item in argostranslate.package.get_available_packages()
            if item.from_code == SOURCE_LANGUAGE and item.to_code == TARGET_LANGUAGE
        ),
        None,
    )
    if package is None:
        raise RuntimeError("Argos Translate en-ru model was not found")
    argostranslate.package.install_from_path(package.download())
    if not model_is_installed():
        raise RuntimeError("Argos Translate en-ru model installation failed")
    print("Argos Translate en-ru model installed.")


if __name__ == "__main__":
    main()
