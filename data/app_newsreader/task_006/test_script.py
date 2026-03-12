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


print("[Newsreader Task 006] Start test: offline save/cache to SD")
take_screenshot("00_app_launched")

article = find_element_smart([
    (AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().className("android.widget.ListView").childSelector(new UiSelector().index(0))'),
    (AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().className("androidx.recyclerview.widget.RecyclerView")'),
], timeout=8)
if article:
    article.click()
take_screenshot("01_article_opened_or_home")

offline_btn = find_element_smart([
    (AppiumBy.ACCESSIBILITY_ID, "Download"),
    (AppiumBy.ACCESSIBILITY_ID, "Offline"),
    (AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().textContains("Offline")'),
    (AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().textContains("Download")'),
    (AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().textContains("Cache")'),
], timeout=8)

if not offline_btn:
    more = find_element_smart([
        (AppiumBy.ACCESSIBILITY_ID, "More options"),
        (AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().descriptionContains("More")'),
    ], timeout=4)
    if more:
        more.click()
        take_screenshot("02_overflow_opened")
    offline_btn = find_element_smart([
        (AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().textContains("Offline")'),
        (AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().textContains("Download")'),
        (AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().textContains("Cache")'),
    ], timeout=5)

if not offline_btn:
    raise Exception("Cannot find offline save/download/cache button.")

offline_btn.click()
take_screenshot("03_offline_button_clicked")

status = find_element_smart([
    (AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().textContains("Offline")'),
    (AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().textContains("Cached")'),
    (AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().textContains("Downloaded")'),
    (AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().textContains("SD")'),
], timeout=4)
if not status:
    raise Exception("No visible result indicating offline download/cache behavior.")

take_screenshot("04_offline_status_visible")
print("[Newsreader Task 006] Completed")