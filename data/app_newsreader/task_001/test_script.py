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


def click_if_found(strategies, screenshot_name, timeout=3):
	element = find_element_smart(strategies, timeout=timeout)
	if element:
		element.click()
	take_screenshot(screenshot_name)
	return element is not None


def open_settings_from_overflow_or_fail():
	# Three-dots overflow description is localized on many devices.
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
	take_screenshot("02_open_overflow_menu")

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
	take_screenshot("03_settings_from_menu")


print("[Newsreader Task 001] Start test: Girl Skull Theme")
take_screenshot("00_app_launched")

click_if_found([
	(AppiumBy.ID, "android:id/button1"),
	(AppiumBy.XPATH, "//*[@text='OK' or @text='Allow' or @text='Got it']"),
], "01_after_initial_dialog")

open_settings_from_overflow_or_fail()

theme_entry = find_element_smart([
	(AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().textContains("Theme")'),
	(AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().textContains("主题")'),
	(AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().textContains("Appearance")'),
	(AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().textContains("Personal")'),
], timeout=6)
if not theme_entry:
	raise Exception("Cannot find Theme or Appearance entry.")
theme_entry.click()
take_screenshot("04_theme_menu_opened")

automatic_toggle = find_element_smart([
	(AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().textContains("Automatic")'),
	(AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().textContains("automatic")'),
	(AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().textContains("自动")'),
	(AppiumBy.XPATH, "//*[contains(@text, 'Automatic') or contains(@text, 'automatic') or contains(@text, '自动')]")
], timeout=6)
if not automatic_toggle:
	raise Exception("Cannot find Automatic theme toggle.")

# Ensure automatic mode is disabled so a manual theme can be selected.
if automatic_toggle.get_attribute("checked") == "true":
	automatic_toggle.click()
	time.sleep(0.5)
take_screenshot("05_automatic_disabled")

target_theme = find_element_smart([
	(AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().textContains("Pink Skull")'),
	(AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().textContains("pink skull")'),
	(AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().textContains("粉")'),
	(AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().textContains("Skull")'),
], timeout=6)
if not target_theme:
	# Some builds keep theme options collapsed under the current value (e.g., white).
	current_theme_value = find_element_smart([
		(AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().textContains("white")'),
		(AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().textContains("White")'),
		(AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().textContains("default")'),
		(AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().textContains("Default")'),
		(AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().textContains("粉")'),
		(AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().className("android.widget.Spinner")'),
		(AppiumBy.XPATH, "//android.widget.Spinner | //android.widget.TextView[contains(@text,'white') or contains(@text,'White') or contains(@text,'default') or contains(@text,'Default')]")
	], timeout=6)
	if not current_theme_value:
		raise Exception("Cannot find theme selector current value (e.g., white).")
	current_theme_value.click()
	time.sleep(0.6)
	take_screenshot("06_theme_value_expanded")
	target_theme = find_element_smart([
		(AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().textContains("Pink Skull")'),
		(AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().textContains("pink skull")'),
		(AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().textContains("粉")'),
		(AppiumBy.ANDROID_UIAUTOMATOR, 'new UiSelector().textContains("Skull")'),
	], timeout=6)
if not target_theme:
	raise Exception("Cannot find Pink Skull theme option.")
target_theme.click()
take_screenshot("07_pink_skull_theme_selected")

driver.back()
time.sleep(0.8)
driver.back()
time.sleep(0.8)
take_screenshot("08_back_to_home")
take_screenshot("09_final_state")
print("[Newsreader Task 001] Completed")