package com.maksimowiczm.foodyou.app.ui.notifications

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.maksimowiczm.foodyou.common.domain.userpreferences.UserPreferencesRepository
import com.maksimowiczm.foodyou.notifications.domain.entity.NotificationPreferences
import kotlinx.coroutines.flow.SharingStarted
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.flow.stateIn
import kotlinx.coroutines.launch
import kotlinx.coroutines.runBlocking

internal class NotificationSettingsViewModel(
    private val notificationPreferencesRepository: UserPreferencesRepository<NotificationPreferences>
) : ViewModel() {

    private val _preferences = notificationPreferencesRepository.observe()
    val preferences =
        _preferences.stateIn(
            scope = viewModelScope,
            started = SharingStarted.WhileSubscribed(2_000),
            initialValue = runBlocking { _preferences.first() },
        )

    fun updatePreferences(preferences: NotificationPreferences) {
        viewModelScope.launch { notificationPreferencesRepository.update { preferences } }
    }
}
