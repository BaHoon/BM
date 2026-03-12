def find_element_smart(strategies, timeout=6):
    end_time = time.time() + timeout
    while time.time() < end_time:
        for by_type, value in strategies:
            try:
                element = driver.find_element(by_type, value)
                if element:
                    return element
            except Exception:
                pass
    return None


def open_settings_or_fail():
    take_screenshot("01_home")
    # Must click overflow menu (three dots) first to access Settings.
    overflow = find_element_smart([
        (AppiumBy.ID, "android:id/action_overflow_menu"),
        (AppiumBy.ACCESSIBILITY_ID, "More options"),
        (AppiumBy.ACCESSIBILITY_ID, "更多选项"),
        (AppiumBy.ACCESSIBILITY_ID, "更多選項"),
        (AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().descriptionContains("More")'),
        (AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().descriptionContains("more")'),
        (AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().descriptionContains("更多")'),
        (AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().resourceId("android:id/action_overflow_menu")'),
        (AppiumBy.XPATH, "//*[@resource-id='android:id/action_overflow_menu']"),
    ], timeout=10)
    if not overflow:
        raise Exception("Cannot open overflow menu (three dots).")
    overflow.click()
    take_screenshot("02_overflow_opened")
    
    settings_item = find_element_smart([
        (AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().textContains("Settings")'),
        (AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().textContains("settings")'),
        (AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().textContains("设置")'),
        (AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().textContains("設定")'),
        (AppiumBy.XPATH, "//*[contains(@text, 'Settings') or contains(@text, 'settings') or contains(@text, '设置') or contains(@text, '設定')]")
    ], timeout=10)
    if not settings_item:
        raise Exception("Cannot open Settings page.")
    settings_item.click()
    take_screenshot("03_settings_opened")


def try_open_accessibility_or_theme():
    option = find_element_smart([
        (AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().textContains("Accessibility")'),
        (AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().textContains("Theme")'),
        (AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().textContains("Display")'),
        (AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().textContains("Color")'),
    ], timeout=6)
    if not option:
        raise Exception("Cannot find Accessibility/Theme related entry.")
    option.click()
    take_screenshot("04_accessibility_or_theme_opened")


print("[Newsreader Task 002] Start test: color-blind/high-contrast mode")
take_screenshot("00_app_launched")
open_settings_or_fail()
try_open_accessibility_or_theme()

mode_toggle = find_element_smart([
    (AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().textContains("Color blind")'),
    (AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().textContains("High contrast")'),
    (AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().textContains("Contrast")'),
], timeout=8)
if not mode_toggle:
    raise Exception("Cannot find color-blind or high-contrast mode option.")
mode_toggle.click()
take_screenshot("05_mode_enabled")

take_screenshot("06_final_state")
print("[Newsreader Task 002] Completed")