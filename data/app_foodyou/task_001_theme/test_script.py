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

# 6. 点击右上角"设置"的齿轮图标
settings_btn = find_element_smart([
    # 使用用户提供的 accessibility id（语言无关，最优先）
    (AppiumBy.ACCESSIBILITY_ID, "前往设置"),
    (AppiumBy.ACCESSIBILITY_ID, "Settings"),  # 备用英文
    (AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().descriptionContains("设置")'),
    (AppiumBy.XPATH, "//*[contains(@content-desc, 'Settings') or contains(@content-desc, '设置')]"),
    (AppiumBy.XPATH, "//*[@text='设置' or @text='Settings' or @text='設定']"),
])
if settings_btn:
    settings_btn.click()
    take_screenshot("05_settings_opened")
    print("[FoodYou Test] ✓ 已打开设置页面")
else:
    print("[FoodYou Test] ✗ 未找到设置按钮")
    raise Exception("无法找到设置按钮")

# 7. 点击"个性化"
personalization_btn = find_element_smart([
    # TODO: 替换为实际的 resource-id
    # (AppiumBy.ID, "com.darealreally.foodyou:id/personalization_option"),
    (AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().textMatches("个性化|Personalization|個人化").clickable(true)'),
    (AppiumBy.XPATH, "//*[@text='个性化' or @text='Personalization' or @text='個人化']"),
])
if personalization_btn:
    personalization_btn.click()
    take_screenshot("06_personalization")
    print("[FoodYou Test] ✓ 已进入个性化页面")
else:
    print("[FoodYou Test] ✗ 未找到'个性化'选项")
    raise Exception("无法找到个性化选项")

# 8. 点击"颜色"
color_btn = find_element_smart([
    # TODO: 替换为实际的 resource-id
    # (AppiumBy.ID, "com.darealreally.foodyou:id/color_option"),
    (AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().textMatches("颜色|Color|カラー").clickable(true)'),
    (AppiumBy.XPATH, "//*[@text='颜色' or @text='Color' or @text='色' or @text='Colour']"),
])
if color_btn:
    color_btn.click()
    take_screenshot("07_color_selection")
    print("[FoodYou Test] ✓ 已进入颜色选择页面")
else:
    print("[FoodYou Test] ✗ 未找到'颜色'选项")
    raise Exception("无法找到颜色选项")

# 9. 点击"少女骷髅" (Girly Skull)
girly_skull_btn = find_element_smart([
    # TODO: 替换为实际的 resource-id
    # (AppiumBy.ID, "com.darealreally.foodyou:id/theme_girly_skull"),
    (AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().textMatches("少女骷髅|Girly Skull").clickable(true)'),
    (AppiumBy.XPATH, "//*[@text='少女骷髅' or @text='Girly Skull']"),
])
if girly_skull_btn:
    girly_skull_btn.click()
    take_screenshot("08_girly_skull_selected")
    print("[FoodYou Test] ✓ 已选择'少女骷髅'主题")
else:
    print("[FoodYou Test] ✗ 未找到'少女骷髅'选项")
    raise Exception("无法找到少女骷髅主题")



print("[FoodYou Test] 测试脚本执行完毕")

# 注意：
# 1. 不要在脚本中调用 driver.quit()，appium_runner 会自动清理
# 2. 使用 take_screenshot("名称") 而非 driver.save_screenshot()
# 3. 如果测试失败需要让流水线感知，可以 raise Exception("错误原因")