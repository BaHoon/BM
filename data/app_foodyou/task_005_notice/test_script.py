# ========================================
# FoodYou App - Task 005: Notification Settings
# ========================================
# 简化测试脚本：直达设置页面，验证"通知设置"功能
# 运行环境中已注入以下全局变量：
#   - driver: Appium WebDriver 实例
#   - take_screenshot(name): 自动保存截图
#   - AppiumBy: 元素定位器
#   - time: Python time 模块
# ========================================

def find_element_smart(strategies, timeout=5):
    """
    智能元素定位：尝试多种策略
    
    Args:
        strategies: 策略列表，按优先级排序 [(by_type, value), ...]
        timeout: 超时时间（秒）
    
    Returns:
        找到的元素，如果都失败则返回 None
    """
    import time
    end_time = time.time() + timeout
    
    while time.time() < end_time:
        for by_type, value in strategies:
            try:
                element = driver.find_element(by_type, value)
                if element:
                    return element
            except:
                continue
    return None

print("[Task 005] 开始执行通知设置测试...")

# ============================================
# Step 1: 等待应用启动并关闭引导页
# ============================================
take_screenshot("00_app_launched")
print("[Task 005] ✓ 应用已启动")

# 关闭可能的引导弹窗（"好的"按钮）
ok_btn = find_element_smart([
    (AppiumBy.ID, "android:id/button1"),
    (AppiumBy.XPATH, "//*[@text='好的' or @text='OK' or @text='確定']"),
], timeout=3)
if ok_btn:
    ok_btn.click()
    print("[Task 005] ✓ 已关闭引导弹窗")

# 关闭可能的同意条款页
agree_btn = find_element_smart([
    (AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().textContains("同意")'),
    (AppiumBy.XPATH, "//*[contains(@text, '同意') or contains(@text, 'Agree')]"),
], timeout=3)
if agree_btn:
    agree_btn.click()
    print("[Task 005] ✓ 已同意条款")

# 跳过引导教程
skip_btn = find_element_smart([
    (AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().textMatches("跳过|Skip")'),
    (AppiumBy.XPATH, "//*[@text='跳过' or @text='Skip']"),
], timeout=3)
if skip_btn:
    skip_btn.click()
    print("[Task 005] ✓ 已跳过教程")

# 关闭版本更新提示
close_update = find_element_smart([
    (AppiumBy.ACCESSIBILITY_ID, "关闭工作表"),
    (AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().className("android.widget.ImageButton")'),
], timeout=3)
if close_update:
    close_update.click()
    print("[Task 005] ✓ 已关闭更新提示")

take_screenshot("01_home_page")

# ============================================
# Step 2: 直达设置页面 (The Shortcut!)
# ============================================
print("[Task 005] 正在导航到设置页面...")

# 查找设置图标（齿轮图标，通常在右上角）
settings_btn = find_element_smart([
    (AppiumBy.ACCESSIBILITY_ID, "前往设置"),
    (AppiumBy.ACCESSIBILITY_ID, "Settings"),
    (AppiumBy.ACCESSIBILITY_ID, "设置"),
    (AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().descriptionContains("设置")'),
    (AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().descriptionContains("Settings")'),
    (AppiumBy.XPATH, "//*[contains(@content-desc, 'Settings') or contains(@content-desc, '设置')]"),
], timeout=8)

if settings_btn:
    settings_btn.click()
    take_screenshot("02_settings_page")
    print("[Task 005] ✓ 已打开设置页面")
else:
    print("[Task 005] ⚠️ 未找到设置按钮")
    take_screenshot("02_settings_not_found")
    raise Exception("无法找到设置入口")

# ============================================
# Step 3: 查找并进入"通知设置"菜单
# ============================================
print("[Task 005] 查找并进入'通知设置'菜单...")

# 查找"通知设置" / "Notification Settings"菜单项
notification_settings = find_element_smart([
    (AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().textContains("通知")'),
    (AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().textContains("Notification")'),
    (AppiumBy.XPATH, "//*[contains(@text, '通知') or contains(@text, 'Notification')]"),
    (AppiumBy.XPATH, "//*[contains(@text, '提醒') or contains(@text, 'Reminder')]"),
], timeout=5)

if notification_settings:
    print("[Task 005] ✓ 找到'通知设置'菜单项")
    
    # 点击进入通知设置页面
    notification_settings.click()
    take_screenshot("03_notification_settings_page")
    print("[Task 005] ✓ 已进入通知设置页面")
    
else:
    print("[Task 005] ⚠️ 未找到'通知设置'菜单")
    # 即使没找到，也截图当前设置页面供VLM评判
    take_screenshot("03_settings_no_notification_menu")

# ============================================
# Step 4: 最终截图
# ============================================
take_screenshot("04_final_state")
print("[Task 005] ✓ 测试完成")

print("[Task 005] 测试脚本执行完毕")

# 注意：
# 1. 不要调用 driver.quit()，appium_runner 会自动清理
# 2. 如果测试失败需要中断，可以 raise Exception("错误原因")
# 3. 核心思路：快速跳过引导 → 直达设置 → 验证通知菜单