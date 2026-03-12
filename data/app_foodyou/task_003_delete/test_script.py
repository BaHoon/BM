# ========================================
# FoodYou App 自动化测试脚本
# ========================================
# 此脚本由 appium_runner.py 自动加载并执行
# 运行环境中已注入以下全局变量：
#   - driver: Appium WebDriver 实例（已连接到 MuMu 模拟器 127.0.0.1:7555）
#   - take_screenshot(name): 自动保存截图到结果目录
#   - AppiumBy: 元素定位器（XPATH, ID, ANDROID_UIAUTOMATOR 等）
#   - time: Python time 模块
# ========================================

def find_element_smart(strategies):
    """
    智能元素定位：尝试多种策略，优先使用语言无关的方式
    
    Args:
        strategies: 策略列表，按优先级排序
                   每个策略是 (by_type, value) 元组
    
    Returns:
        找到的元素，如果都失败则返回 None
    """
    for by_type, value in strategies:
        try:
            element = driver.find_element(by_type, value)
            return element
        except:
            continue
    return None

print("[FoodYou Test] 开始执行 FoodYou 自动化测试...")

# 1. 等待应用完全加载
take_screenshot("00_app_launched")
print("[FoodYou Test] ✓ 应用已启动")

# 2. 点击"好的"按钮（示例：多语言兼容定位）
ok_btn = find_element_smart([
    # 策略1: 使用 resource-id（语言无关，最优先）
    (AppiumBy.ID, "android:id/button1"),  # Android 默认对话框的确定按钮 ID
    # 策略2: 使用 UiAutomator 的 Button 类型 + 索引（第一个按钮）
    (AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().className("android.widget.Button").index(0)'),
    # 策略3: 使用文本（多语言备选）
    (AppiumBy.XPATH, "//*[@text='好的' or @text='OK' or @text='確定']"),
])
if ok_btn:
    ok_btn.click()
    take_screenshot("01_after_ok")
    print("[FoodYou Test] ✓ 已点击'好的/OK'")
else:
    print("[FoodYou Test] ⚠️ 未找到'好的'按钮，可能已跳过")

# 3. 点击"我同意，继续"
agree_btn = find_element_smart([
    # TODO: 请使用 uiautomatorviewer 查看实际的 resource-id 并替换
    # (AppiumBy.ID, "com.darealreally.foodyou:id/agree_button"),
    (AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().textContains("同意").clickable(true)'),
    (AppiumBy.XPATH, "//*[contains(@text, '同意') or contains(@text, 'Agree') or contains(@text, '同意する')]"),
])
if agree_btn:
    agree_btn.click()
    take_screenshot("02_after_agree")
    print("[FoodYou Test] ✓ 已点击'我同意，继续'")
else:
    print("[FoodYou Test] ⚠️ 未找到'我同意，继续'按钮")

# 4. 点击右上角的"跳过"按钮
skip_btn = find_element_smart([
    # TODO: 替换为实际的 resource-id
    # (AppiumBy.ID, "com.darealreally.foodyou:id/skip_button"),
    (AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().textMatches("跳过|Skip|スキップ").clickable(true)'),
    (AppiumBy.XPATH, "//*[@text='跳过' or @text='Skip' or @text='スキップ']"),
])
if skip_btn:
    skip_btn.click()
    take_screenshot("03_after_skip")
    print("[FoodYou Test] ✓ 已点击'跳过'")
else:
    print("[FoodYou Test] ⚠️ 未找到'跳过'按钮")

# 5. 点击屏幕中上处离开版本更新页面
close_update = find_element_smart([
    # 使用用户提供的 accessibility id（语言无关，最优先）
    (AppiumBy.ACCESSIBILITY_ID, "关闭工作表"),
    (AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().className("android.widget.ImageButton").clickable(true)'),
    (AppiumBy.XPATH, "//*[contains(@text, '取消') or contains(@text, 'Cancel') or contains(@text, '稍后') or contains(@text, 'Later')]"),
])
if close_update:
    close_update.click()
    take_screenshot("04_after_version_update")
    print("[FoodYou Test] ✓ 已关闭版本更新页面")
else:
    # 方式2: 点击屏幕上方区域（坐标点击，语言无关）
    try:
        screen_size = driver.get_window_size()
        x = screen_size['width'] // 2
        y = screen_size['height'] // 4
        driver.tap([(x, y)])
        take_screenshot("04_after_version_update")
        print("[FoodYou Test] ✓ 已通过点击屏幕关闭版本更新页面")
    except Exception as e2:
        print(f"[FoodYou Test] ⚠️ 无法关闭版本更新页面: {e2}")

# 6. 点击闪电按钮
lightning_btn = find_element_smart([
    # 使用 UiAutomator 定位第一个 Button
    (AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().className("android.widget.Button").instance(0)'),
])
if lightning_btn:
    lightning_btn.click()
    take_screenshot("05_lightning_clicked")
    print("[FoodYou Test] ✓ 已点击闪电按钮")
else:
    print("[FoodYou Test] ✗ 未找到闪电按钮")
    raise Exception("无法找到闪电按钮")

# 7. 在名称中输入 egg
name_input = find_element_smart([
    # 尝试多种定位策略找到输入框
    (AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().className("android.widget.EditText").instance(0)'),
    (AppiumBy.XPATH, "//android.widget.EditText"),
    (AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().className("android.widget.EditText")'),
])
if name_input:
    name_input.clear()
    name_input.send_keys("egg")
    take_screenshot("06_egg_input")
    print("[FoodYou Test] ✓ 已输入 'egg'")
else:
    print("[FoodYou Test] ✗ 未找到名称输入框")
    raise Exception("无法找到名称输入框")

# 8. 点击右上角的保存按钮
save_btn = find_element_smart([
    (AppiumBy.ACCESSIBILITY_ID, "保存"),
    (AppiumBy.ACCESSIBILITY_ID, "Save"),
    (AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().descriptionMatches("保存|Save")'),
    (AppiumBy.XPATH, "//*[contains(@content-desc, '保存') or contains(@content-desc, 'Save')]"),
    (AppiumBy.XPATH, "//*[@text='保存' or @text='Save']"),
])
if save_btn:
    save_btn.click()
    take_screenshot("07_saved")
    print("[FoodYou Test] ✓ 已点击保存按钮")
else:
    print("[FoodYou Test] ✗ 未找到保存按钮")
    raise Exception("无法找到保存按钮")

# 9. 点击刚刚被添加的 egg
egg_item = find_element_smart([
    (AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().textContains("egg")'),
    (AppiumBy.XPATH, "//*[contains(@text, 'egg')]"),
])
if egg_item:
    egg_item.click()
    take_screenshot("08_egg_clicked")
    print("[FoodYou Test] ✓ 已点击 egg 条目")
else:
    print("[FoodYou Test] ✗ 未找到 egg 条目")
    raise Exception("无法找到 egg 条目")

# 10. 点击删除条目
delete_item_btn = find_element_smart([
    (AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().textMatches("删除条目|Delete.*|刪除.*").clickable(true)'),
    (AppiumBy.XPATH, "//*[contains(@text, '删除') or contains(@text, 'Delete') or contains(@text, '刪除')]"),
    (AppiumBy.ACCESSIBILITY_ID, "删除条目"),
    (AppiumBy.ACCESSIBILITY_ID, "Delete entry"),
])
if delete_item_btn:
    delete_item_btn.click()
    take_screenshot("09_delete_item_clicked")
    print("[FoodYou Test] ✓ 已点击删除条目")
else:
    print("[FoodYou Test] ✗ 未找到删除条目按钮")
    raise Exception("无法找到删除条目按钮")

# 11. 点击删除（确认删除）
confirm_delete_btn = find_element_smart([
    (AppiumBy.ID, "android:id/button1"),  # Android 默认对话框的确定按钮
    (AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().textMatches("删除|Delete|確定").clickable(true)'),
    (AppiumBy.XPATH, "//*[@text='删除' or @text='Delete' or @text='確定' or @text='OK']"),
])
if confirm_delete_btn:
    confirm_delete_btn.click()
    take_screenshot("10_deleted")
    print("[FoodYou Test] ✓ 已确认删除")
else:
    print("[FoodYou Test] ✗ 未找到确认删除按钮")
    raise Exception("无法找到确认删除按钮")



print("[FoodYou Test] 测试脚本执行完毕")

# 注意：
# 1. 不要在脚本中调用 driver.quit()，appium_runner 会自动清理
# 2. 使用 take_screenshot("名称") 而非 driver.save_screenshot()
# 3. 如果测试失败需要让流水线感知，可以 raise Exception("错误原因")