package com.maksimowiczm.foodyou.app.ui.notifications

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.LargeFlexibleTopAppBar
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.material3.TopAppBarDefaults
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.input.nestedscroll.nestedScroll
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.maksimowiczm.foodyou.app.ui.common.component.ArrowBackIconButton
import com.maksimowiczm.foodyou.app.ui.common.component.SettingsListItem
import foodyou.app.generated.resources.*
import org.jetbrains.compose.resources.stringResource
import org.koin.compose.viewmodel.koinViewModel

@Composable
internal fun NotificationSettingsRoute(
    onBack: () -> Unit,
    modifier: Modifier = Modifier,
    viewModel: NotificationSettingsViewModel = koinViewModel(),
) {
    val preferences by viewModel.preferences.collectAsStateWithLifecycle()

    NotificationSettingsScreen(
        notificationsEnabled = preferences.notificationsEnabled,
        onNotificationsEnabledChange = { enabled ->
            viewModel.updatePreferences(preferences.copy(notificationsEnabled = enabled))
        },
        mealRemindersEnabled = preferences.mealRemindersEnabled,
        onMealRemindersEnabledChange = { enabled ->
            viewModel.updatePreferences(preferences.copy(mealRemindersEnabled = enabled))
        },
        goalRemindersEnabled = preferences.goalRemindersEnabled,
        onGoalRemindersEnabledChange = { enabled ->
            viewModel.updatePreferences(preferences.copy(goalRemindersEnabled = enabled))
        },
        importExportNotificationsEnabled = preferences.importExportNotificationsEnabled,
        onImportExportNotificationsEnabledChange = { enabled ->
            viewModel.updatePreferences(preferences.copy(importExportNotificationsEnabled = enabled))
        },
        onBack = onBack,
        modifier = modifier,
    )
}

@Composable
internal fun NotificationSettingsScreen(
    notificationsEnabled: Boolean,
    onNotificationsEnabledChange: (Boolean) -> Unit,
    mealRemindersEnabled: Boolean,
    onMealRemindersEnabledChange: (Boolean) -> Unit,
    goalRemindersEnabled: Boolean,
    onGoalRemindersEnabledChange: (Boolean) -> Unit,
    importExportNotificationsEnabled: Boolean,
    onImportExportNotificationsEnabledChange: (Boolean) -> Unit,
    onBack: () -> Unit,
    modifier: Modifier = Modifier,
) {
    val scrollBehavior = TopAppBarDefaults.exitUntilCollapsedScrollBehavior()

    Scaffold(
        modifier = modifier,
        topBar = {
            LargeFlexibleTopAppBar(
                title = { Text(stringResource(Res.string.headline_notification_settings)) },
                navigationIcon = { ArrowBackIconButton(onBack) },
                scrollBehavior = scrollBehavior,
            )
        },
    ) { paddingValues ->
        Column(
            modifier = Modifier
                .fillMaxSize()
                .nestedScroll(scrollBehavior.nestedScrollConnection)
                .verticalScroll(rememberScrollState())
                .padding(paddingValues)
                .padding(vertical = 8.dp),
            verticalArrangement = Arrangement.spacedBy(0.dp),
        ) {
            SettingsListItem(
                label = { Text(stringResource(Res.string.headline_enable_notifications)) },
                onClick = { onNotificationsEnabledChange(!notificationsEnabled) },
                supportingContent = { Text(stringResource(Res.string.description_enable_notifications)) },
                trailingContent = { Switch(checked = notificationsEnabled, onCheckedChange = null) },
            )

            SettingsListItem(
                label = { Text(stringResource(Res.string.headline_meal_reminders)) },
                onClick = { onMealRemindersEnabledChange(!mealRemindersEnabled) },
                supportingContent = { Text(stringResource(Res.string.description_meal_reminders)) },
                trailingContent = { Switch(checked = mealRemindersEnabled, onCheckedChange = null, enabled = notificationsEnabled) },
            )

            SettingsListItem(
                label = { Text(stringResource(Res.string.headline_goal_reminders)) },
                onClick = { onGoalRemindersEnabledChange(!goalRemindersEnabled) },
                supportingContent = { Text(stringResource(Res.string.description_goal_reminders)) },
                trailingContent = { Switch(checked = goalRemindersEnabled, onCheckedChange = null, enabled = notificationsEnabled) },
            )

            SettingsListItem(
                label = { Text(stringResource(Res.string.headline_import_export_notifications)) },
                onClick = { onImportExportNotificationsEnabledChange(!importExportNotificationsEnabled) },
                supportingContent = { Text(stringResource(Res.string.description_import_export_notifications)) },
                trailingContent = { Switch(checked = importExportNotificationsEnabled, onCheckedChange = null, enabled = notificationsEnabled) },
            )
        }
    }
}
