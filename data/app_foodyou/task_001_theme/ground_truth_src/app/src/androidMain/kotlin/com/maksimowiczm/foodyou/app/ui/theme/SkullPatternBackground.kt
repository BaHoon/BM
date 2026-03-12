package com.maksimowiczm.foodyou.app.ui.theme

import androidx.compose.foundation.Canvas
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.Path
import androidx.compose.ui.graphics.drawscope.Fill
import androidx.compose.ui.graphics.drawscope.DrawScope

@Composable
internal fun SkullPatternBackground(content: @Composable () -> Unit) {
    Box(modifier = Modifier.fillMaxSize()) {
        content()
        
        Canvas(modifier = Modifier.fillMaxSize()) {
            val skullColor = Color(0xFFFFFFFF).copy(alpha = 0.35f) // 白色骷髅，透明度 35%
            val spacing = 280f
            val cols = (size.width / spacing).toInt() + 2
            val rows = (size.height / spacing).toInt() + 2

            for (row in 0 until rows) {
                for (col in 0 until cols) {
                    val x = col * spacing + (if (row % 2 == 0) 0f else spacing / 2)
                    val y = row * spacing
                    
                    if (x < size.width + spacing && y < size.height + spacing) {
                        drawSkull(
                            center = Offset(x, y),
                            size = 45f,
                            color = skullColor
                        )
                    }
                }
            }
        }
    }
}

private fun DrawScope.drawSkull(center: Offset, size: Float, color: Color) {
    // 绘制骷髅头
    drawCircle(
        color = color,
        radius = size * 0.5f,
        center = center,
        style = Fill
    )
    
    // 绘制眼睛（两个圆形）- 更明显
    val eyeRadius = size * 0.13f
    val eyeOffsetY = -size * 0.1f
    val eyeOffsetX = size * 0.22f
    
    // 左眼
    drawCircle(
        color = Color(0xFFFF1493).copy(alpha = 0.7f),
        radius = eyeRadius,
        center = Offset(center.x - eyeOffsetX, center.y + eyeOffsetY),
        style = Fill
    )
    
    // 右眼
    drawCircle(
        color = Color(0xFFFF1493).copy(alpha = 0.7f),
        radius = eyeRadius,
        center = Offset(center.x + eyeOffsetX, center.y + eyeOffsetY),
        style = Fill
    )
    
    // 绘制鼻子（倒立的心形）
    val nosePath = Path().apply {
        val noseSize = size * 0.15f
        val noseY = center.y + size * 0.05f
        moveTo(center.x, noseY - noseSize * 0.3f)
        lineTo(center.x - noseSize * 0.3f, noseY + noseSize * 0.2f)
        lineTo(center.x + noseSize * 0.3f, noseY + noseSize * 0.2f)
        close()
    }
    drawPath(
        path = nosePath,
        color = Color(0xFFFF1493).copy(alpha = 0.7f),
        style = Fill
    )
    
    // 绘制可爱的笑脸（小圆点）
    val mouthY = center.y + size * 0.25f
    val mouthDotRadius = size * 0.045f
    val mouthSpacing = size * 0.12f
    
    for (i in -2..2) {
        drawCircle(
            color = Color(0xFFFF1493).copy(alpha = 0.7f),
            radius = mouthDotRadius,
            center = Offset(center.x + i * mouthSpacing, mouthY),
            style = Fill
        )
    }
    
    // 绘制蝴蝶结（少女元素）- 更鲜艳
    val bowY = center.y - size * 0.52f
    val bowSize = size * 0.28f
    
    // 左侧蝴蝶结
    drawCircle(
        color = Color(0xFFFF69B4).copy(alpha = 0.6f),
        radius = bowSize,
        center = Offset(center.x - bowSize * 0.7f, bowY),
        style = Fill
    )
    
    // 右侧蝴蝶结
    drawCircle(
        color = Color(0xFFFF69B4).copy(alpha = 0.6f),
        radius = bowSize,
        center = Offset(center.x + bowSize * 0.7f, bowY),
        style = Fill
    )
    
    // 蝴蝶结中心
    drawCircle(
        color = Color(0xFFFF1493).copy(alpha = 0.8f),
        radius = bowSize * 0.45f,
        center = Offset(center.x, bowY),
        style = Fill
    )
}
