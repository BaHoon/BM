import json
import re
from pathlib import Path

root = Path(r"d:/benchmark/benchmark_root")
data_dir = root / "data"

appium_calls = re.compile(r"\(\s*AppiumBy\.([A-Z_]+)\s*,\s*([^)]+?)\)")
text_contains_pat = re.compile(r"textContains\(\"([^\"]+)\"\)")
text_eq_pat = re.compile(r"text\(\"([^\"]+)\"\)")
desc_contains_pat = re.compile(r"descriptionContains\(\"([^\"]+)\"\)")

created = []
for script in data_dir.glob("**/test_script.py"):
    task_dir = script.parent
    out_path = task_dir / "ui_checks.json"

    text = script.read_text(encoding="utf-8", errors="replace")
    matches = appium_calls.findall(text)

    checks = []

    any_of_main = []
    for by, raw in matches:
        raw_clean = raw.strip()
        raw_clean = raw_clean.split("#", 1)[0].strip().rstrip(",")
        if raw_clean.startswith('"') and raw_clean.endswith('"'):
            val = raw_clean[1:-1]
        elif raw_clean.startswith("'") and raw_clean.endswith("'"):
            val = raw_clean[1:-1]
        else:
            continue

        by = by.upper()
        if by == "ID":
            any_of_main.append({"resource_id": val})
        elif by == "ACCESSIBILITY_ID":
            any_of_main.append({"content_desc": val})
        elif by == "ANDROID_UIAUTOMATOR":
            tc = text_contains_pat.search(val)
            te = text_eq_pat.search(val)
            dc = desc_contains_pat.search(val)
            if tc:
                any_of_main.append({"text_contains": tc.group(1)})
            elif te:
                any_of_main.append({"text": te.group(1)})
            elif dc:
                any_of_main.append({"content_desc_contains": dc.group(1)})
        elif by == "XPATH":
            m1 = re.search(r"@text\s*=\s*'([^']+)'", val)
            m2 = re.search(r"contains\(@text,\s*'([^']+)'\)", val)
            m3 = re.search(r"contains\(@content-desc,\s*'([^']+)'\)", val)
            if m1:
                any_of_main.append({"text": m1.group(1)})
            elif m2:
                any_of_main.append({"text_contains": m2.group(1)})
            elif m3:
                any_of_main.append({"content_desc_contains": m3.group(1)})

    seen = set()
    dedup_any_of = []
    for item in any_of_main:
        key = json.dumps(item, ensure_ascii=False, sort_keys=True)
        if key in seen:
            continue
        seen.add(key)
        dedup_any_of.append(item)

    if dedup_any_of:
        checks.append({
            "name": "core_target_element_exists",
            "any_of": dedup_any_of[:10],
            "min_count": 1
        })

    checks.append({
        "name": "clickable_widget_present",
        "any_of": [
            {"class_name": "android.widget.Button", "clickable": True},
            {"class_name": "android.widget.ImageButton", "clickable": True},
            {"class_name": "android.widget.TextView", "clickable": True}
        ],
        "min_count": 1
    })

    payload = {
        "version": 1,
        "notes": "Auto-generated from test_script selectors; please refine for task-specific strictness.",
        "checks": checks,
    }

    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    created.append(str(out_path.relative_to(root)).replace('\\','/'))

print(f"generated={len(created)}")
for p in created:
    print(p)
