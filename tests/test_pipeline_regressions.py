from scripts.Env_Manager import EnvManager
from scripts.llm_client import _parse_json_safe
from scripts.logger import ExperimentLogger


def test_nested_golden_build_prefix_is_removed():
    manager = EnvManager.__new__(EnvManager)
    command = manager._resolve_build_command(
        {
            "build_command": (
                "cd golden_src/timeto.me-main && "
                "./gradlew :android_app:assembleBaseDebug"
            )
        }
    )
    assert command == "./gradlew :android_app:assembleBaseDebug"


def test_fenced_visual_judge_json_is_parsed():
    raw = '```json\n{"passed": true, "checks": []}\n```'
    assert _parse_json_safe(raw, {"passed": False})["passed"] is True


def test_markdown_cell_escapes_table_delimiters_and_newlines():
    assert ExperimentLogger._md_cell("left|right\nnext") == r"left\|right next"


def test_markdown_cell_is_bounded():
    assert len(ExperimentLogger._md_cell("x" * 1000)) == 600


def test_evaluator_uses_explicit_workspace_for_isolated_results(tmp_path):
    from scripts.Evaluator import Evaluator

    evaluator = Evaluator(
        data_dir=str(tmp_path / "data"),
        results_dir=str(tmp_path / "results" / "golden_validation"),
        workspace_dir=str(tmp_path / "workspace"),
    )
    assert evaluator.workspace_dir == tmp_path / "workspace"


def test_shifted_warning_line_inherited_from_base_is_not_introduced(tmp_path):
    base = tmp_path / "base.kt"
    candidate = tmp_path / "candidate.kt"
    base.write_text("first\ndeprecatedCall()\n")
    candidate.write_text("newLine()\nfirst\ndeprecatedCall()\n")
    assert EnvManager._warning_line_is_unchanged(candidate, base, 3)


def test_crawler_penalizes_generic_menu_distractions():
    from scripts.appium_runner import AppiumRunner

    xml = """<hierarchy>
      <node clickable="true" enabled="true" displayed="true" bounds="[0,0][50,50]"
            content-desc="Browser menu" resource-id="pkg:id/aiChatIconMenu" />
      <node clickable="true" enabled="true" displayed="true" bounds="[50,0][100,50]"
            content-desc="More options" resource-id="pkg:id/browserMenu" />
    </hierarchy>"""
    runner = AppiumRunner.__new__(AppiumRunner)
    runner.driver = None
    runner._appium_process = None
    runner._emulator_process = None
    runner._appium_log_handle = None
    runner._emulator_log_handle = None
    chosen = runner._choose_click_candidate(
        xml,
        {"navigation_terms": ["menu", "more", "settings"], "score_terms": []},
        set(),
    )
    assert "browserMenu" in chosen["label"]


def test_popup_ok_does_not_match_bookmarks():
    from scripts.appium_runner import AppiumRunner

    assert not AppiumRunner._label_contains_term("bookmarks pkg:id/bookmarks", "ok")
    assert AppiumRunner._label_contains_term("ok pkg:id/confirm", "ok")


def test_prompt_theme_is_used_only_as_navigation_hint():
    from scripts.tools.level2_utils import build_level2_spec

    spec = build_level2_spec({"prompt": 'Add a new "Girly Skull" theme in theme settings.'})
    assert "theme" in spec["navigation_terms"]
    assert "appearance" in spec["navigation_terms"]
    assert "theme" not in spec["target_phrases"]


def test_generic_setting_suffix_does_not_beat_visible_theme_row():
    from scripts.appium_runner import AppiumRunner

    xml = """<hierarchy>
      <node clickable="true" enabled="true" displayed="true" bounds="[0,0][100,40]"
            resource-id="pkg:id/setAsDefaultBrowserSetting" />
      <node clickable="true" enabled="true" displayed="true" bounds="[0,40][100,80]"
            text="App Theme" resource-id="pkg:id/appThemeSetting" />
    </hierarchy>"""
    runner = AppiumRunner.__new__(AppiumRunner)
    runner.driver = None
    runner._appium_process = runner._emulator_process = None
    runner._appium_log_handle = runner._emulator_log_handle = None
    chosen = runner._choose_click_candidate(
        xml,
        {"navigation_terms": ["setting", "theme"], "score_terms": []},
        set(),
    )
    assert chosen["label"].startswith("App Theme")


def test_post_target_visual_state_only_applies_theme_tasks(tmp_path):
    from scripts.appium_runner import AppiumRunner

    runner = AppiumRunner.__new__(AppiumRunner)
    runner.driver = None
    runner._appium_process = runner._emulator_process = None
    runner._appium_log_handle = runner._emulator_log_handle = None
    assert runner._capture_post_target_visual_state(
        "<hierarchy />", {}, {"prompt": "Add a toolbar button"}, tmp_path
    ) is None


def test_theme_confirmation_prefers_set_theme_over_cancel():
    from scripts.appium_runner import AppiumRunner

    xml = """<hierarchy>
      <node clickable="true" enabled="true" displayed="true" bounds="[0,0][80,40]" text="Cancel" />
      <node clickable="true" enabled="true" displayed="true" bounds="[80,0][200,40]" text="Set Theme" />
    </hierarchy>"""
    runner = AppiumRunner.__new__(AppiumRunner)
    runner.driver = None
    runner._appium_process = runner._emulator_process = None
    runner._appium_log_handle = runner._emulator_log_handle = None
    assert runner._choose_confirmation_candidate(xml)["label"] == "Set Theme"


def test_background_image_routes_through_appearance_without_stopwords():
    from scripts.tools.level2_utils import build_level2_spec

    spec = build_level2_spec({"prompt": "Let users upload the app's overall background image."})
    assert "appearance" in spec["navigation_terms"]
    assert "overall" not in spec["score_terms"]
    assert "the" not in spec["score_terms"]


def test_level2_match_keeps_node_bounds_for_screenshot_repositioning():
    from scripts.tools.level2_utils import match_target_xml

    result = match_target_xml(
        '<hierarchy><node text="Background Image" bounds="[0,1700][900,1850]" /></hierarchy>',
        {"target_phrases": ["Background Image"], "explicit_target_phrases": ["Background Image"], "match_attributes": ["text"]},
    )
    assert result["matched_nodes"][0]["bounds"] == "[0,1700][900,1850]"
