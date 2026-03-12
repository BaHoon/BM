package com.maksimowiczm.foodyou.app.ui.settings

import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.outlined.Notifications
import androidx.compose.material3.Icon
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.Shape
import com.maksimowiczm.foodyou.app.ui.common.component.SettingsListItem
import foodyou.app.generated.resources.*
import org.jetbrains.compose.resources.stringResource

@Composable
fun NotificationSettingsListItem(
    onClick: () -> Unit,
    modifier: Modifier = Modifier,
    shape: Shape = androidx.compose.material3.MaterialTheme.shapes.medium,
    color: Color = androidx.compose.material3.MaterialTheme.colorScheme.surface,
    contentColor: Color = androidx.compose.material3.MaterialTheme.colorScheme.onSurface,
) {
    SettingsListItem(
        icon = { Icon(Icons.Outlined.Notifications, null) },
        label = { Text(stringResource(Res.string.headline_notification_settings)) },
        supportingContent = { Text(stringResource(Res.string.description_manage_notifications)) },
        onClick = onClick,
        modifier = modifier,
        shape = shape,
        color = color,
        contentColor = contentColor,
    )
}
