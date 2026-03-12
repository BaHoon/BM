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


print("[Newsreader Task 010] Start test: paragraph spacing setting")
take_screenshot("00_app_launched")

settings = find_element_smart([
    (AppiumBy.ACCESSIBILITY_ID, "Settings"),
    (AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().descriptionContains("Settings")'),
], timeout=8)
if not settings:
    raise Exception("Cannot find Settings button.")
settings.click()
take_screenshot("01_settings_opened")

text_menu = find_element_smart([
    (AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().textContains("Text")'),
    (AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().textContains("Display")'),
    (AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().textContains("Reader")'),
], timeout=8)
if not text_menu:
    raise Exception("Cannot find text/display settings entry.")
text_menu.click()
take_screenshot("02_text_settings_opened")

spacing_entry = find_element_smart([
    (AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().textContains("Paragraph spacing")'),
    (AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().textContains("Line spacing")'),
    (AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().textContains("Spacing")'),
], timeout=8)
if not spacing_entry:
    raise Exception("Cannot find paragraph/line spacing setting.")
spacing_entry.click()
take_screenshot("03_spacing_options_opened")

large_option = find_element_smart([
    (AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().textContains("Large")'),
    (AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().textContains("Larger")'),
], timeout=6)
if not large_option:
    raise Exception("Cannot find larger paragraph spacing option.")
large_option.click()
take_screenshot("04_large_spacing_selected")

take_screenshot("05_final_state")
print("[Newsreader Task 010] Completed")