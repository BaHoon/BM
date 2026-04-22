import json
from pathlib import Path

root = Path(r"d:/benchmark/benchmark_root")
app_package_fallback = {
    "app_newsreader": "livio.rssreader",
    "app_foodyou": "com.example.foodyou",
    "app_todoagenda": "com.example.todoagenda",
}

generic_tokens = {
    "ok", "settings", "more options", "更多选项", "更多選項", "android:id/button1",
    "android:id/action_overflow_menu", "allow", "got it", "skip", "跳过"
}

updated = []
for p in root.glob("data/**/ui_checks.json"):
    parts = p.parts
    # .../data/app_xxx/task_xxx/ui_checks.json
    try:
        data_idx = parts.index("data")
        app_name = parts[data_idx + 1]
        task_id = parts[data_idx + 2]
    except Exception:
        continue

    payload = json.loads(p.read_text(encoding="utf-8"))
    checks = payload.get("checks", []) if isinstance(payload, dict) else []

    core = None
    for c in checks:
        if isinstance(c, dict) and c.get("name") == "core_target_element_exists":
            core = c
            break
    if core is None:
        core = {
            "name": "core_target_element_exists",
            "any_of": [],
            "min_count": 1,
        }

    any_of = core.get("any_of", []) if isinstance(core.get("any_of"), list) else []
    normalized_any_of = [x for x in any_of if isinstance(x, dict)]
    core["any_of"] = normalized_any_of[:12]
    core["min_count"] = 1

    # Build task-specific marker check from extracted literals in core any_of.
    marker_any_of = []
    for m in normalized_any_of:
        if "text" in m:
            v = str(m["text"]).strip()
            if v and v.lower() not in generic_tokens:
                marker_any_of.append({"text_contains": v})
        if "text_contains" in m:
            v = str(m["text_contains"]).strip()
            if v and v.lower() not in generic_tokens:
                marker_any_of.append({"text_contains": v})
        if "content_desc" in m:
            v = str(m["content_desc"]).strip()
            if v and v.lower() not in generic_tokens:
                marker_any_of.append({"content_desc_contains": v})
        if "content_desc_contains" in m:
            v = str(m["content_desc_contains"]).strip()
            if v and v.lower() not in generic_tokens:
                marker_any_of.append({"content_desc_contains": v})
        if "resource_id" in m:
            rid = str(m["resource_id"]).strip()
            if rid and not rid.startswith("android:id/"):
                marker_any_of.append({"resource_id": rid})

    # De-dup markers.
    seen = set()
    marker_dedup = []
    for item in marker_any_of:
        k = json.dumps(item, ensure_ascii=False, sort_keys=True)
        if k in seen:
            continue
        seen.add(k)
        marker_dedup.append(item)

    # Resolve package constraint.
    meta_path = p.parent / "meta.json"
    package_name = None
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            package_name = meta.get("app_package")
        except Exception:
            package_name = None
    if not package_name:
        package_name = app_package_fallback.get(app_name, "")

    strict_checks = [
        core,
        {
            "name": "interactable_nodes_at_least_two",
            "any_of": [
                {"class_name": "android.widget.Button", "clickable": True, "enabled": True},
                {"class_name": "android.widget.ImageButton", "clickable": True, "enabled": True},
                {"class_name": "android.widget.TextView", "clickable": True, "enabled": True}
            ],
            "min_count": 2
        },
    ]

    if package_name:
        strict_checks.append(
            {
                "name": "app_package_present",
                "match": {"package": package_name},
                "min_count": 1,
            }
        )

    if marker_dedup:
        strict_checks.append(
            {
                "name": "task_specific_marker_exists",
                "any_of": marker_dedup[:8],
                "min_count": 1,
            }
        )

    payload["version"] = 2
    payload["notes"] = (
        "Strict profile auto-generated: core element + interactable count + package consistency "
        "+ task-specific marker."
    )
    payload["checks"] = strict_checks

    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    updated.append(str(p.relative_to(root)).replace('\\', '/'))

print(f"updated={len(updated)}")
for x in updated:
    print(x)
