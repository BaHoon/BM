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


print("[Newsreader Task 009] Start test: add Korean language option")
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

korean = None
for index in range(6):
    korean = find_element_smart([
        (AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().textContains("Korean")'),
    ], timeout=2)
    if korean:
        break
    scroll_down_once()
    take_screenshot(f"03_scroll_{index + 1}")

if not korean:
    raise Exception("Cannot find Korean language option.")

korean.click()
take_screenshot("04_korean_selected")
take_screenshot("05_final_state")
print("[Newsreader Task 009] Completed")