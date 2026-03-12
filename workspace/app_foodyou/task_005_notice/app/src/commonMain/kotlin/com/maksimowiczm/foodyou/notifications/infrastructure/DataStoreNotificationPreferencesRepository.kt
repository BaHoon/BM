package com.maksimowiczm.foodyou.notifications.infrastructure

import androidx.datastore.core.DataStore
import androidx.datastore.preferences.core.MutablePreferences
import androidx.datastore.preferences.core.Preferences
import androidx.datastore.preferences.core.booleanPreferencesKey
import com.maksimowiczm.foodyou.common.infrastructure.datastore.AbstractDataStoreUserPreferencesRepository
import com.maksimowiczm.foodyou.notifications.domain.entity.NotificationPreferences

internal class DataStoreNotificationPreferencesRepository(dataStore: DataStore<Preferences>) :
    AbstractDataStoreUserPreferencesRepository<NotificationPreferences>(dataStore) {
    
    override fun Preferences.toUserPreferences(): NotificationPreferences =
        NotificationPreferences(
            notificationsEnabled = this[NotificationPreferencesKeys.notificationsEnabled] ?: false,
            mealRemindersEnabled = this[NotificationPreferencesKeys.mealRemindersEnabled] ?: false,
            goalRemindersEnabled = this[NotificationPreferencesKeys.goalRemindersEnabled] ?: false,
            importExportNotificationsEnabled = this[NotificationPreferencesKeys.importExportNotificationsEnabled] ?: true,
        )

    override fun MutablePreferences.applyUserPreferences(updated: NotificationPreferences) {
        this[NotificationPreferencesKeys.notificationsEnabled] = updated.notificationsEnabled
        this[NotificationPreferencesKeys.mealRemindersEnabled] = updated.mealRemindersEnabled
        this[NotificationPreferencesKeys.goalRemindersEnabled] = updated.goalRemindersEnabled
        this[NotificationPreferencesKeys.importExportNotificationsEnabled] = updated.importExportNotificationsEnabled
    }
}

private object NotificationPreferencesKeys {
    val notificationsEnabled = booleanPreferencesKey("notifications:enabled")
    val mealRemindersEnabled = booleanPreferencesKey("notifications:meal_reminders_enabled")
    val goalRemindersEnabled = booleanPreferencesKey("notifications:goal_reminders_enabled")
    val importExportNotificationsEnabled = booleanPreferencesKey("notifications:import_export_enabled")
}
