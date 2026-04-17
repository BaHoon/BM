# Teammate JSON Onboarding Guide

This guide explains how to plug teammate deliverables into the current pipeline with no code changes.

## 1) Required Deliverables

For each app, provide two files:

1. `[app]_golden_mapping.json`
2. `[app]_ui_verification.json`

Examples:

- `foodyou_golden_mapping.json`
- `foodyou_ui_verification.json`

## 2) Where To Put Them

Either location works:

1. App-level data folder (recommended)
- `data/app_foodyou/foodyou_golden_mapping.json`
- `data/app_foodyou/foodyou_ui_verification.json`

2. Repository root (global fallback)
- `foodyou_golden_mapping.json`
- `foodyou_ui_verification.json`

## 3) Key Matching Rules

Task keys are matched loosely (case-insensitive, ignores `_` and `-`).

Examples considered equal:

- `FoodYou_Thm_01`
- `foodyou-thm-01`
- `foodyouthm01`

## 4) JSON Schemas

### 4.1 Golden Mapping

```json
{
  "FoodYou_Thm_01": {
    "modified_files": [
      "app/src/main/res/layout/activity_settings.xml",
      "app/src/main/java/com/app/SettingsActivity.java"
    ],
    "total_modified_count": 2
  }
}
```

### 4.2 UI Verification

```json
{
  "FoodYou_Thm_01": {
    "target_ui_verification": {
      "type": "xpath",
      "value": "//android.widget.Button[@content-desc='深色']",
      "action": "exists",
      "expected_attributes": {
        "enabled": "true",
        "displayed": "true"
      }
    }
  }
}
```

Notes:

- `target_ui_verification.expected_attributes` is supported directly.
- If both `expected_attributes` and `expected_attrs` are provided, `expected_attributes` takes precedence.

## 5) How Pipeline Uses Them

- L2: reads `target_ui_verification` for XPath + optional attribute checks.
- Recall fallback: reads `modified_files` from golden mapping first.
  - If golden mapping is missing, pipeline falls back to diffing `ground_truth_src`.

## 6) Validate Quickly

Run one task:

```bash
python scripts/global_evaluator.py FoodYou_Thm_01 --model deepseek-r1 --strategy ReAct
```

Run batch:

```bash
python scripts/Experiment_Launcher.py --model deepseek-r1 --strategy direct ReAct tool_planning --tasks app_foodyou --direct-repeats 3
```

Check outputs:

- `results/<app>/<task>/scorer_result.json`
- `results/raw_data.jsonl`

## 7) Common Mistakes

- Wrong task key spelling: use the same domain key naming per task.
- Using non-project paths in `modified_files`: paths should be relative to project root in workspace.
- XPath too broad: prefer `resource-id`/`content-desc` based unique selectors.
