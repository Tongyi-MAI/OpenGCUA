"""Validate translated UI coverage and formatting before packaging."""
import json
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]


def catalogue(language):
    lines = (ROOT / f"Resources/{language}.lproj/Localizable.strings").read_text().splitlines()
    result = {}
    for line in lines:
        if not line or line.startswith('/*'): continue
        match = re.fullmatch(r'("(?:\\.|[^"\\])*") = ("(?:\\.|[^"\\])*");', line)
        if not match: raise AssertionError(f"Invalid strings entry: {line}")
        key, value = map(json.loads, match.groups())
        if key in result: raise AssertionError(f"Duplicate translation: {key}")
        result[key] = value
    return result


class LocalizationTests(unittest.TestCase):
    def test_languages_have_matching_keys_and_format_arguments(self):
        english, chinese = catalogue('en'), catalogue('zh-Hans')
        self.assertEqual(set(english), set(chinese))
        for key, value in english.items():
            self.assertEqual(key.count('%@'), value.count('%@'), key)
            self.assertNotRegex(value, r'[\u4e00-\u9fff]', key)
            self.assertTrue(value.strip(), key)

    def test_every_literal_localization_key_has_an_english_translation(self):
        english = catalogue('en')
        for source in (ROOT/'Sources').glob('*.swift'):
            for match in re.finditer(r'\bL\(("(?:\\.|[^"\\])*")', source.read_text()):
                self.assertIn(json.loads(match.group(1)), english, source.name)

    def test_native_permission_prompts_are_translated(self):
        for language in ('en', 'zh-Hans'):
            text = (ROOT/f'Resources/{language}.lproj/InfoPlist.strings').read_text()
            for key in ('NSMicrophoneUsageDescription', 'NSScreenCaptureUsageDescription', 'NSSpeechRecognitionUsageDescription'):
                self.assertIn(key, text)


if __name__ == '__main__': unittest.main()
