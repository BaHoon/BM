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


print("[Newsreader Task 005] Start test: page-turn animation options")
take_screenshot("00_app_launched")

settings = find_element_smart([
    (AppiumBy.ACCESSIBILITY_ID, "Settings"),
    (AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().descriptionContains("Settings")'),
], timeout=8)
if not settings:
    raise Exception("Cannot find Settings button.")
settings.click()
take_screenshot("01_settings_opened")

animation_menu = find_element_smart([
    (AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().textContains("Animation")'),
    (AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().textContains("Page")'),
    (AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().textContains("Transition")'),
    (AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().textContains("Reader")'),
], timeout=8)
if not animation_menu:
    raise Exception("Cannot find page-turn animation setting entry.")
animation_menu.click()
take_screenshot("02_animation_menu_opened")

effect = find_element_smart([
    (AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().textContains("Slide")'),
    (AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().textContains("Fade")'),
    (AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().textContains("Flip")'),
    (AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().textContains("Curl")'),
], timeout=8)
if not effect:
    raise Exception("Cannot find any page-turn animation effect option.")
effect.click()
take_screenshot("03_animation_effect_selected")

take_screenshot("04_final_state")
print("[Newsreader Task 005] Completed")