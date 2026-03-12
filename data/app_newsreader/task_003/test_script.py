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


def open_settings():
    settings = find_element_smart([
        (AppiumBy.ACCESSIBILITY_ID, "Settings"),
        (AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().descriptionContains("Settings")'),
    ], timeout=8)
    if settings:
        settings.click()
        take_screenshot("02_settings_opened")
        return True
    more = find_element_smart([
        (AppiumBy.ACCESSIBILITY_ID, "More options"),
        (AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().descriptionContains("More")'),
    ], timeout=4)
    if more:
        more.click()
        take_screenshot("02_overflow_opened")
    settings_item = find_element_smart([
        (AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().textContains("Settings")'),
    ], timeout=4)
    if settings_item:
        settings_item.click()
        take_screenshot("03_settings_opened")
        return True
    return False


print("[Newsreader Task 003] Start test: upload custom background image")
take_screenshot("00_app_launched")
take_screenshot("01_home")

if not open_settings():
    raise Exception("Cannot open Settings page.")

bg_menu = find_element_smart([
    (AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().textContains("Background")'),
    (AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().textContains("Wallpaper")'),
    (AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().textContains("Appearance")'),
], timeout=8)
if not bg_menu:
    raise Exception("Cannot find background settings entry.")
bg_menu.click()
take_screenshot("04_background_settings_opened")

upload_btn = find_element_smart([
    (AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().textContains("Upload")'),
    (AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().textContains("Choose")'),
    (AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().textContains("Pick image")'),
    (AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().textContains("Select image")'),
], timeout=8)
if not upload_btn:
    raise Exception("Cannot find upload/select image button.")
upload_btn.click()
take_screenshot("05_upload_button_clicked")

file_picker = find_element_smart([
    (AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().textContains("Files")'),
    (AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().textContains("Photos")'),
    (AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().textContains("Images")'),
    (AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().textContains("Gallery")'),
], timeout=5)
if not file_picker:
    raise Exception("Image picker did not appear after tapping upload/select image.")
take_screenshot("06_file_picker_visible")

take_screenshot("07_final_state")
print("[Newsreader Task 003] Completed")