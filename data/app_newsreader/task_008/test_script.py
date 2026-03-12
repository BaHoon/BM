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


def scroll_down_once():
    size = driver.get_window_size()
    x = size["width"] // 2
    start_y = int(size["height"] * 0.8)
    end_y = int(size["height"] * 0.3)
    driver.swipe(x, start_y, x, end_y, 350)


print("[Newsreader Task 008] Start test: add Bengali language option")
take_screenshot("00_app_launched")

settings = find_element_smart([
    (AppiumBy.ACCESSIBILITY_ID, "Settings"),
    (AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().descriptionContains("Settings")'),
], timeout=8)
if not settings:
    raise Exception("Cannot find Settings button.")
settings.click()
take_screenshot("01_settings_opened")

language_menu = find_element_smart([
    (AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().textContains("Language")'),
    (AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().textContains("Locale")'),
], timeout=8)
if not language_menu:
    raise Exception("Cannot find language settings entry.")
language_menu.click()
take_screenshot("02_language_settings_opened")

bengali = None
for index in range(6):
    bengali = find_element_smart([
        (AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().textContains("Bengali")'),
    ], timeout=2)
    if bengali:
        break
    scroll_down_once()
    take_screenshot(f"03_scroll_{index + 1}")

if not bengali:
    raise Exception("Cannot find Bengali language option.")

bengali.click()
take_screenshot("04_bengali_selected")
take_screenshot("05_final_state")
print("[Newsreader Task 008] Completed")