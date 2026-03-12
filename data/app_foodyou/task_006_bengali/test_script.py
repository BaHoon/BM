# ========================================
# FoodYou App - Task 006: Add Bengali Language
# ========================================
# 简化测试脚本：直达设置页面，验证"语言设置"功能
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

print("[Task 006] 开始执行孟加拉语言设置测试...")

# ============================================
# Step 1: 等待应用启动并关闭引导页
# ============================================
take_screenshot("00_app_launched")
print("[Task 006] ✓ 应用已启动")

# 关闭可能的引导弹窗（"好的"按钮）
ok_btn = find_element_smart([
    (AppiumBy.ID, "android:id/button1"),
    (AppiumBy.XPATH, "//*[@text='好的' or @text='OK' or @text='確定']"),
], timeout=3)
if ok_btn:
    ok_btn.click()
    print("[Task 006] ✓ 已关闭引导弹窗")

# 关闭可能的同意条款页
agree_btn = find_element_smart([
    (AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().textContains("同意")'),
    (AppiumBy.XPATH, "//*[contains(@text, '同意') or contains(@text, 'Agree')]"),
], timeout=3)
if agree_btn:
    agree_btn.click()
    print("[Task 006] ✓ 已同意条款")

# 跳过引导教程
skip_btn = find_element_smart([
    (AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().textMatches("跳过|Skip")'),
    (AppiumBy.XPATH, "//*[@text='跳过' or @text='Skip']"),
], timeout=3)
if skip_btn:
    skip_btn.click()
    print("[Task 006] ✓ 已跳过教程")

# 关闭版本更新提示
close_update = find_element_smart([
    (AppiumBy.ACCESSIBILITY_ID, "关闭工作表"),
    (AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().className("android.widget.ImageButton")'),
], timeout=3)
if close_update:
    close_update.click()
    print("[Task 006] ✓ 已关闭更新提示")

take_screenshot("01_home_page")

# ============================================
# Step 2: 直达设置页面
# ============================================
print("[Task 006] 正在导航到设置页面...")

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
    print("[Task 006] ✓ 已打开设置页面")
else:
    print("[Task 006] ⚠️ 未找到设置按钮")
    take_screenshot("02_settings_not_found")
    raise Exception("无法找到设置入口")

# ============================================
# Step 3: 查找并进入"语言设置"菜单
# ============================================
print("[Task 006] 查找并进入'语言设置'菜单...")

# 查找"语言" / "Language" 菜单项
language_settings = find_element_smart([
    (AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().textContains("语言")'),
    (AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().textContains("Language")'),
    (AppiumBy.XPATH, "//*[contains(@text, '语言') or contains(@text, 'Language') or contains(@text, '言語')]"),
], timeout=5)

if language_settings:
    print("[Task 006] ✓ 找到'语言设置'菜单项")
    
    # 点击进入语言设置页面
    language_settings.click()
    take_screenshot("03_language_settings_page")
    print("[Task 006] ✓ 已进入语言设置页面")
    
    # ============================================
    # Step 3.5: 滚动屏幕查找 Bengali（孟加拉语）
    # ============================================
    print("[Task 006] 滚动屏幕查找 Bengali...")
    
    # 先尝试查找，如果找不到就向下滚动
    bengali_option = None
    max_scrolls = 5  # 最多滚动5次
    
    for scroll_count in range(max_scrolls):
        bengali_option = find_element_smart([
            (AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().textContains("Bengali")'),
            (AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().textContains("bengali")'),
            (AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().textContains("孟加拉")'),
            (AppiumBy.XPATH, "//*[contains(@text, 'Bengali') or contains(@text, 'bengali') or contains(@text, '孟加拉')]"),
            (AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().textContains("বাংলা")'),  # Bengali in Bengali script
        ], timeout=2)
        
        if bengali_option:
            print(f"[Task 006] ✓ 找到 Bengali 选项（滚动 {scroll_count} 次后）")
            break
        else:
            # 向下滚动
            print(f"[Task 006] 未找到 Bengali，向下滚动 ({scroll_count + 1}/{max_scrolls})...")
            try:
                driver.swipe(500, 1500, 500, 500, 400)  # 从下往上滑动
            except Exception as e:
                print(f"[Task 006] 滚动失败: {e}")
                break
    
    if bengali_option:
        bengali_option.click()
        take_screenshot("04_bengali_selected")
        print("[Task 006] ✓ 已选择 Bengali")
    else:
        print("[Task 006] ⚠️ 未找到 Bengali 选项")
        take_screenshot("04_bengali_not_found")
    
else:
    print("[Task 006] ⚠️ 未找到'语言设置'菜单")
    # 即使没找到，也截图当前设置页面供VLM评判
    take_screenshot("03_settings_no_language_menu")

# ============================================
# Step 4: 最终截图
# ============================================
take_screenshot("05_final_state")
print("[Task 006] ✓ 测试完成")

print("[Task 006] 测试脚本执行完毕")

# 注意：
# 1. 不要调用 driver.quit()，appium_runner 会自动清理
# 2. 使用 take_screenshot("名称") 而非 driver.save_screenshot()
# 3. 如果测试失败需要让流水线感知，可以 raise Exception("错误原因")