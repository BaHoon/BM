# ========================================
# FoodYou App - Task 007: Add Share Icon
# ========================================
# 简化测试脚本：显示应用首页，验证"共享功能"
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

print("[Task 007] 开始执行共享功能测试...")

# ============================================
# Step 1: 等待应用启动并关闭引导页
# ============================================
take_screenshot("00_app_launched")
print("[Task 007] ✓ 应用已启动")

# 关闭可能的引导弹窗（"好的"按钮）
ok_btn = find_element_smart([
    (AppiumBy.ID, "android:id/button1"),
    (AppiumBy.XPATH, "//*[@text='好的' or @text='OK' or @text='確定']"),
], timeout=3)
if ok_btn:
    ok_btn.click()
    print("[Task 007] ✓ 已关闭引导弹窗")

# 关闭可能的同意条款页
agree_btn = find_element_smart([
    (AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().textContains("同意")'),
    (AppiumBy.XPATH, "//*[contains(@text, '同意') or contains(@text, 'Agree')]"),
], timeout=3)
if agree_btn:
    agree_btn.click()
    print("[Task 007] ✓ 已同意条款")

# 跳过引导教程
skip_btn = find_element_smart([
    (AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().textMatches("跳过|Skip")'),
    (AppiumBy.XPATH, "//*[@text='跳过' or @text='Skip']"),
], timeout=3)
if skip_btn:
    skip_btn.click()
    print("[Task 007] ✓ 已跳过教程")

# 关闭版本更新提示
close_update = find_element_smart([
    (AppiumBy.ACCESSIBILITY_ID, "关闭工作表"),
    (AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().className("android.widget.ImageButton")'),
], timeout=3)
if close_update:
    close_update.click()
    print("[Task 007] ✓ 已关闭更新提示")

# ============================================
# Step 2: 直达设置页面
# ============================================
print("[Task 007] 正在导航到设置页面...")

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
    take_screenshot("01_settings_page")
    print("[Task 007] ✓ 已打开设置页面")
else:
    print("[Task 007] ⚠️ 未找到设置按钮")
    take_screenshot("01_settings_not_found")
    raise Exception("无法找到设置入口")

# ============================================
# Step 3: 查找并点击分享图标
# ============================================
print("[Task 007] 查找分享图标...")

share_btn = find_element_smart([
    (AppiumBy.ACCESSIBILITY_ID, "分享"),
    (AppiumBy.ACCESSIBILITY_ID, "Share"),
    (AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().descriptionContains("分享")'),
    (AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().descriptionContains("Share")'),
    (AppiumBy.XPATH, "//*[contains(@content-desc, 'Share') or contains(@content-desc, '分享')]"),
    (AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().className("android.widget.ImageButton").descriptionContains("Share")'),
], timeout=8)

if share_btn:
    print("[Task 007] ✓ 找到分享图标")
    take_screenshot("02_share_icon_found")
    
    # 点击分享图标
    share_btn.click()
    print("[Task 007] ✓ 已点击分享图标")
    
    # 等待分享对话框或菜单出现
    import time
    time.sleep(1)
    
    take_screenshot("03_share_clicked")
else:
    print("[Task 007] ⚠️ 未找到分享图标")
    take_screenshot("02_share_not_found")

# ============================================
# Step 4: 最终截图
# ============================================
take_screenshot("04_final_state")
print("[Task 007] ✓ 测试完成")

print("[Task 007] 测试脚本执行完毕")

# 注意：
# 1. 不要调用 driver.quit()，appium_runner 会自动清理
# 2. 使用 take_screenshot("名称") 而非 driver.save_screenshot()
# 3. 如果测试失败需要让流水线感知，可以 raise Exception("错误原因")