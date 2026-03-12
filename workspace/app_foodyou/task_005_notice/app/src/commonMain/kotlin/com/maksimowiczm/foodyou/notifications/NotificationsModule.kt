package com.maksimowiczm.foodyou.notifications

import com.maksimowiczm.foodyou.common.infrastructure.koin.userPreferencesRepositoryOf
import com.maksimowiczm.foodyou.notifications.infrastructure.DataStoreNotificationPreferencesRepository
import org.koin.dsl.module

val notificationsModule = module {
    userPreferencesRepositoryOf(::DataStoreNotificationPreferencesRepository)
}
