package com.maksimowiczm.foodyou.app.ui.notifications

import com.maksimowiczm.foodyou.common.infrastructure.koin.userPreferencesRepository
import org.koin.core.module.Module
import org.koin.core.module.dsl.viewModel

internal fun Module.notifications() {
    viewModel { NotificationSettingsViewModel(userPreferencesRepository()) }
}
