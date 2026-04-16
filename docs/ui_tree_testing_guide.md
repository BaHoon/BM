# UI 树校验使用说明（Appium + Declarative Checks）

本项目已支持在现有截图测试之外，自动导出 UI 树（DOM tree）并执行声明式断言。

## 1. 运行时行为
每次执行 `AppiumRunner.run_test()` 时会自动：

1. 启动后导出 UI 树到 `results/<app>/<task>/ui_trees/00_app_launched.xml`
2. 执行 `test_script.py`（保持原有流程）
3. 如果 `data/<app>/<task>/ui_checks.json` 存在：
   - 导出 `90_post_script.xml`
   - 按规则执行 UI 树断言
4. 结束前导出 `99_final_state.xml`
5. 失败时尽力导出 `98_failure.xml`

`appium_result.json` 会新增字段：
- `ui_trees`: 导出的 XML 列表
- `ui_checks`: 声明式校验结果（若未配置则为 `no_ui_checks_config`）

## 2. task 级配置文件
在任务目录新增：

`data/<app>/<task>/ui_checks.json`

示例：

```json
{
  "checks": [
    {
      "name": "settings_button_exists",
      "any_of": [
        {"resource_id": "android:id/action_overflow_menu"},
        {"content_desc_contains": "settings"},
        {"text_contains": "设置"}
      ],
      "min_count": 1
    },
    {
      "name": "clickable_button_present",
      "match": {
        "class_name": "android.widget.Button",
        "clickable": true
      },
      "min_count": 1
    }
  ]
}
```

## 3. 支持的匹配字段
单个 matcher 可使用：
- `resource_id`
- `text`
- `text_contains`
- `class_name`
- `content_desc`
- `content_desc_contains`
- `clickable`
- `enabled`
- `package`
- `bounds_contains`

说明：
- `any_of`: 多个 matcher，任意一个达到 `min_count` 即通过该检查。
- `match`: 单 matcher（等价于 `any_of` 只有一项）。
- `min_count`: 最少匹配数量，默认 1。
- `max_count`: 可选上限。

## 4. 与 test_script 的关系
- 不需要为每个任务重写完整自动化脚本。
- 推荐做法：
  - 保留通用交互流程在 `test_script.py`
  - task 差异放到 `ui_checks.json` 断言

## 5. 在脚本中手动使用
`test_script.py` 运行环境已注入：
- `dump_ui_tree(name)`
- `run_ui_tree_checks(tree_name="assertion")`

可在关键步骤手动导出 UI 树，或中途执行一次规则校验。
