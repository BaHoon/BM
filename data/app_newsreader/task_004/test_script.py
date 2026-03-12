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


print("[Newsreader Task 004] Start test: direct search button")
take_screenshot("00_app_launched")

search_btn = find_element_smart([
    (AppiumBy.ACCESSIBILITY_ID, "Search"),
    (AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().descriptionContains("Search")'),
    (AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().textContains("Search")'),
], timeout=10)

if not search_btn:
    more = find_element_smart([
        (AppiumBy.ACCESSIBILITY_ID, "More options"),
        (AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().descriptionContains("More")'),
    ], timeout=4)
    if more:
        more.click()
        take_screenshot("01_overflow_opened")
    search_btn = find_element_smart([
        (AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().textContains("Search")'),
    ], timeout=4)

if not search_btn:
    raise Exception("Cannot find search button.")

search_btn.click()
take_screenshot("02_search_opened")

input_box = find_element_smart([
    (AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().className("android.widget.EditText")'),
    (AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().textContains("Search")'),
], timeout=5)
if not input_box:
    raise Exception("Cannot find search input field after opening search.")

input_box.click()
input_box.clear()
input_box.send_keys("android")
take_screenshot("03_keyword_entered")

driver.press_keycode(66)
take_screenshot("04_search_results")
print("[Newsreader Task 004] Completed")