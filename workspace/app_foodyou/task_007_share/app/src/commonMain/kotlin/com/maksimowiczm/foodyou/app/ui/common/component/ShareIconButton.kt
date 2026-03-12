package com.maksimowiczm.foodyou.app.ui.common.component

import androidx.compose.foundation.interaction.MutableInteractionSource
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Share
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.IconButtonColors
import androidx.compose.material3.IconButtonDefaults
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Shape
import foodyou.app.generated.resources.*
import org.jetbrains.compose.resources.stringResource

/**
 * Share icon button component for sharing current page content
 */
@Composable
fun ShareIconButton(
    onClick: () -> Unit,
    modifier: Modifier = Modifier,
    enabled: Boolean = true,
    colors: IconButtonColors = IconButtonDefaults.iconButtonColors(),
    interactionSource: MutableInteractionSource? = null,
    shape: Shape = IconButtonDefaults.standardShape,
) {
    IconButton(
        onClick = onClick,
        modifier = modifier,
        enabled = enabled,
        colors = colors,
        interactionSource = interactionSource,
        shape = shape,
    ) {
        Icon(
            imageVector = Icons.Default.Share,
            contentDescription = stringResource(Res.string.action_share),
        )
    }
}
