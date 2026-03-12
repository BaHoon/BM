package com.maksimowiczm.foodyou.common.share

import androidx.compose.runtime.staticCompositionLocalOf

/**
 * CompositionLocal for accessing ShareManager in Compose UI
 */
val LocalShareManager = staticCompositionLocalOf<ShareManager?> { null }
