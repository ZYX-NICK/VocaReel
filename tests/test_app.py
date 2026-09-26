from __future__ import annotations

import json
import io
import queue
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app import SeriesEnglishApp
from core import (dictionary_links, import_ecdict_csv, language_hint, lookup_word,
                  split_bilingual_ass, suggest_tracks)
from mpv_client import MpvClient
from preferences import DEFAULT_SHORTCUTS, mpv_key, shortcut_from_event, tkinter_sequence, validate_shortcuts


PROJECT = Path(__file__).resolve().parents[1]


class SubtitleTests(unittest.TestCase):
    def test_split_two_line_ass_keeps_english_chinese_and_timing(self) -> None:
        text = (PROJECT / "samples/sample_bilingual.ass").read_text(encoding="utf-8")
        result = split_bilingual_ass(text)
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.bilingual_events, 3)
        self.assertIn("Hello, my friend", result.english)
        self.assertNotIn("你好", result.english)
        self.assertIn("你好，朋友", result.chinese)
        self.assertNotIn("Hello", result.chinese)
        self.assertIn("0:00:08.20,0:00:12.50", result.english)

    def test_ambiguous_same_line_is_not_partially_hidden(self) -> None:
        source = (PROJECT / "samples/sample_bilingual.ass").read_text(encoding="utf-8")
        source = source.replace("again.\\N先听", "again.先听")
        self.assertIsNone(split_bilingual_ass(source))

    def test_detect_embedded_tracks_and_filename_hints(self) -> None:
        tracks = [
            {"id": 1, "type": "sub", "lang": "eng", "title": "English"},
            {"id": 2, "type": "sub", "lang": "zho", "title": "Chinese"},
        ]
        self.assertEqual(suggest_tracks(tracks), (1, 2))
        self.assertEqual(language_hint({"title": "My.Series.S01E01.zh.ass"}), "zh")
        self.assertEqual(language_hint({"title": "My.Series.S01E01.en.ass"}), "en")

    def test_dictionary_keeps_multiple_parts_of_speech_and_senses(self) -> None:
        payload = [{"word": "bank", "phonetic": "bæŋk", "meanings": [
            {"partOfSpeech": "noun", "definitions": [
                {"definition": "A financial institution.", "example": "I went to the bank."},
                {"definition": "Land alongside a river."}]},
            {"partOfSpeech": "verb", "definitions": [
                {"definition": "To deposit money."}]},
        ]}]
        def fake_json(url: str, _: float) -> object:
            if "dictionaryapi.dev" in url:
                return payload
            if "q=bank&" in url:
                return {"responseStatus": 200, "responseData": {"translatedText": "银行"},
                        "matches": [{"segment": "bank", "translation": "河岸"},
                                    {"segment": "bank holiday", "translation": "银行假日"}]}
            return {"responseStatus": 200, "responseData": {"translatedText": "存入；金融机构"}}
        with patch("core._get_json", side_effect=fake_json):
            entry = lookup_word("Bank", db_path=PROJECT / "tests" / "missing.sqlite")
        self.assertEqual(entry.chinese, ("银行", "河岸"))
        self.assertEqual(tuple(group.part_of_speech for group in entry.meanings), ("noun", "verb"))
        self.assertEqual(len(entry.meanings[0].senses), 2)
        self.assertEqual(entry.meanings[0].senses[1].chinese, "存入；金融机构")
        self.assertEqual(entry.meanings[0].senses[0].example, "I went to the bank.")
        self.assertEqual(entry.meanings[1].senses[0].chinese, "存入；金融机构")

    def test_dictionary_fallback_and_links(self) -> None:
        def fake_json(url: str, _: float) -> object:
            if "dictionaryapi.dev" in url:
                raise OSError("lookup unavailable")
            return {"responseStatus": 200, "responseData": {"translatedText": "银行；河岸"}}
        with patch("core._get_json", side_effect=fake_json):
            entry = lookup_word("bank", db_path=PROJECT / "tests" / "missing.sqlite")
        self.assertEqual(entry.chinese, ("银行；河岸",))
        self.assertIn("word=bank&lang=en", dictionary_links("bank")["有道词典"])
        self.assertTrue(dictionary_links("bank")["剑桥词典"].endswith("/bank"))
        with self.assertRaises(ValueError):
            dictionary_links("../bank")

    def test_imported_ecdict_shows_multiple_chinese_senses_offline(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "ecdict.csv"
            database = Path(directory) / "ecdict.sqlite3"
            source.write_text(
                'word,phonetic,definition,translation\n'
                'bank,bæŋk,"n. a financial institution","n. 银行；河岸\nv. 存钱；倾斜"\n',
                encoding="utf-8",
            )
            self.assertEqual(import_ecdict_csv(source, database), 1)
            with patch("core._get_json", side_effect=AssertionError("offline entry must avoid network")) as network:
                entry = lookup_word("bank", db_path=database)
            network.assert_not_called()
            self.assertEqual(entry.chinese_groups[0].senses, ("银行", "河岸"))
            self.assertEqual(entry.chinese_groups[1].senses, ("存钱", "倾斜"))
            self.assertEqual(entry.meanings[0].senses[0].definition, "n. a financial institution")

    @unittest.skipUnless(shutil.which("ffprobe"), "ffprobe is not installed")
    def test_embedded_sample_contains_two_ass_tracks(self) -> None:
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "s", "-show_entries",
             "stream=codec_name:stream_tags=language", "-of", "json",
             str(PROJECT / "samples/sample_embedded.mkv")],
            check=True, capture_output=True, text=True,
        )
        streams = json.loads(probe.stdout)["streams"]
        self.assertEqual([s["codec_name"] for s in streams], ["ass", "ass"])
        self.assertEqual([s["tags"]["language"] for s in streams], ["eng", "zho"])

class MpvProtocolTests(unittest.TestCase):
    def test_wire_command_is_utf8_json_line(self) -> None:
        client = MpvClient(lambda _: None)
        stream = io.BytesIO()
        client.stream = stream
        client.ready = True
        writer = threading.Thread(target=client._write_loop, daemon=True)
        writer.start()
        self.assertTrue(client.send("sub-add", "C:/剧集/英语.ass", "auto", "English", "en"))
        client.outgoing.put(None)
        writer.join(timeout=1)
        self.assertEqual(
            json.loads(stream.getvalue().decode("utf-8")),
            {"command": ["sub-add", "C:/剧集/英语.ass", "auto", "English", "en"]},
        )

    def test_slow_pipe_does_not_block_ui_send(self) -> None:
        gate = threading.Event()
        class SlowStream:
            def write(self, _: bytes) -> None:
                gate.wait(timeout=1)
            def flush(self) -> None:
                pass
        client = MpvClient(lambda _: None)
        client.stream = SlowStream()
        client.ready = True
        writer = threading.Thread(target=client._write_loop, daemon=True)
        writer.start()
        start = time.monotonic()
        try:
            self.assertTrue(client.send("set_property", "sid", 2))
            self.assertLess(time.monotonic() - start, 0.1)
        finally:
            gate.set()
            client.outgoing.put(None)
            writer.join(timeout=1)

    def test_switch_keeps_english_and_only_toggles_chinese(self) -> None:
        class Sink:
            def __init__(self):
                self.commands = []

            def send(self, *command):
                self.commands.append(command)

        class Value:
            def set(self, _):
                pass

        class Button:
            def config(self, **_):
                pass
            configure = config

        app = SeriesEnglishApp.__new__(SeriesEnglishApp)
        app.client = Sink()
        app.zh_var = Value()
        app.mode_button = Button()
        app.selected_en, app.selected_zh = 1, 2
        app.chinese = "你好"
        app.mode_bilingual = False
        app.applied_track_state = None
        app.shortcuts = DEFAULT_SHORTCUTS.copy()
        app._apply_tracks()
        self.assertIn(("set_property", "sid", 1), app.client.commands)
        self.assertIn(("set_property", "secondary-sid", 2), app.client.commands)
        self.assertIn(("set_property", "secondary-sub-visibility", False), app.client.commands)
        app.client.commands.clear()
        app.mode_bilingual = True
        app._apply_tracks()
        self.assertIn(("set_property", "secondary-sub-visibility", True), app.client.commands)

    def test_unmodified_track_list_does_not_reset_open_selector(self) -> None:
        class Combo:
            def __init__(self):
                self.changes = 0
            def __setitem__(self, _, __):
                self.changes += 1
            def set(self, _):
                self.changes += 1
        class Sink:
            def send(self, *_):
                pass
        class Value:
            def set(self, _):
                pass
        class Button:
            def configure(self, **_):
                pass
        app = SeriesEnglishApp.__new__(SeriesEnglishApp)
        app.client = Sink()
        app.shortcuts = DEFAULT_SHORTCUTS.copy()
        app.en_choice, app.zh_choice = Combo(), Combo()
        app.mode_button, app.zh_var = Button(), Value()
        app.label_to_id, app.track_signature, app.tracks = {}, (), []
        app.selected_en = app.selected_zh = None
        app.preferred_en_path = app.preferred_zh_path = None
        app.mode_bilingual, app.chinese = False, ""
        app.applied_track_state = None
        tracks = [{"id": 1, "type": "sub", "lang": "eng", "selected": True}]
        app._update_tracks(tracks)
        before = app.en_choice.changes
        app._update_tracks([{**tracks[0], "selected": False}])
        self.assertEqual(app.en_choice.changes, before)

    def test_player_hotkey_updates_mode_in_panel(self) -> None:
        class Value:
            def set(self, _):
                pass
        class Button:
            def configure(self, **_):
                pass
        class Root:
            def after(self, *_) -> None:
                pass
        app = SeriesEnglishApp.__new__(SeriesEnglishApp)
        app.root, app.mode_button, app.zh_var = Root(), Button(), Value()
        app.shortcuts = DEFAULT_SHORTCUTS.copy()
        app.selected_en, app.selected_zh = 1, 2
        app.mode_bilingual, app.chinese = False, "你好"
        app.applied_track_state = None
        app.events = queue.Queue()
        app.events.put({"event": "property-change", "name": "secondary-sub-visibility", "data": True})
        app._drain_events()
        self.assertTrue(app.mode_bilingual)
        self.assertEqual(app.applied_track_state, (1, 2, True))


class PreferenceTests(unittest.TestCase):
    def test_shortcuts_convert_for_both_windows(self) -> None:
        self.assertEqual(tkinter_sequence("Ctrl+B"), "<Control-b>")
        self.assertEqual(mpv_key("Ctrl+B"), "Ctrl+b")
        self.assertEqual(validate_shortcuts(DEFAULT_SHORTCUTS), DEFAULT_SHORTCUTS)

    def test_duplicate_shortcuts_are_rejected(self) -> None:
        settings = {**DEFAULT_SHORTCUTS, "open_subtitle": "Ctrl+O"}
        with self.assertRaises(ValueError):
            validate_shortcuts(settings)

    def test_captured_key_and_mpv_rebinding_swap(self) -> None:
        self.assertEqual(shortcut_from_event(SimpleNamespace(keysym="b", state=0x4)), "Ctrl+B")
        class Sink:
            def __init__(self):
                self.commands = []
            def send(self, *command):
                self.commands.append(command)
        app = SeriesEnglishApp.__new__(SeriesEnglishApp)
        app.client = Sink()
        app.shortcuts = {**DEFAULT_SHORTCUTS, "seek_back": "Right", "seek_forward": "Left"}
        app.mpv_shortcuts = {action: DEFAULT_SHORTCUTS[action] for action in
                             ("toggle_subtitles", "play_pause", "seek_back", "seek_forward")}
        app._install_mpv_shortcuts()
        commands = app.client.commands
        self.assertLess(commands.index(("keybind", "LEFT", "ignore")),
                        commands.index(("keybind", "LEFT", "seek 5 relative")))
        self.assertEqual(commands[-1], ("keybind", "LEFT", "seek 5 relative"))

    def test_ass_parse_runs_outside_ui_callback(self) -> None:
        class Value:
            def set(self, _):
                pass
        app = SeriesEnglishApp.__new__(SeriesEnglishApp)
        app.video_path = "the_current_video"
        app.status_var = Value()
        app.events = queue.Queue()
        gate = threading.Event()
        with patch("app.split_bilingual_ass", side_effect=lambda _: (gate.wait(timeout=2), None)[1]):
            start = time.monotonic()
            try:
                app.load_subtitle(str(PROJECT / "samples/sample_bilingual.ass"))
                self.assertLess(time.monotonic() - start, 0.1)
            finally:
                gate.set()
            self.assertEqual(app.events.get(timeout=2)["event"], "subtitle-ready")

    def test_real_ass_is_prepared_in_background(self) -> None:
        class Value:
            def set(self, _):
                pass
        with tempfile.TemporaryDirectory() as directory:
            app = SeriesEnglishApp.__new__(SeriesEnglishApp)
            app.video_path = "the_current_video"
            app.status_var = Value()
            app.events = queue.Queue()
            app.tempdir = SimpleNamespace(name=directory)
            app.load_subtitle(str(PROJECT / "samples/sample_bilingual.ass"))
            event = app.events.get(timeout=2)
            self.assertEqual(event["event"], "subtitle-split-ready")
            self.assertTrue(Path(event["en"]).is_file())
            self.assertTrue(Path(event["zh"]).is_file())

    def test_word_window_topmost_setting_is_applied(self) -> None:
        class Window:
            def __init__(self):
                self.last = None
            def attributes(self, *args):
                self.last = args
        app = SeriesEnglishApp.__new__(SeriesEnglishApp)
        app.word_window = Window()
        app.word_topmost = SimpleNamespace(get=lambda: True)
        saved = []
        app._save_config = lambda: saved.append(True)
        app._set_word_topmost()
        self.assertEqual(app.word_window.last, ("-topmost", True))
        self.assertEqual(saved, [True])


if __name__ == "__main__":
    unittest.main()
