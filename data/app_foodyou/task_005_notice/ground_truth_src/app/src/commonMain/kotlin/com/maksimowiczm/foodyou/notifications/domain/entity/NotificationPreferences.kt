package com.maksimowiczm.foodyou.notifications.domain.entity

import com.maksimowiczm.foodyou.common.domain.userpreferences.UserPreferences

data class NotificationPreferences(
    val notificationsEnabled: Boolean = false,
    val mealRemindersEnabled: Boolean = false,
    val goalRemindersEnabled: Boolean = false,
    val importExportNotificationsEnabled: Boolean = true,
) : UserPreferences
