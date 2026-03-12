package com.maksimowiczm.foodyou.common.share

import android.content.Context
import android.content.Intent

/**
 * Android implementation of ShareManager
 */
actual class ShareManager(private val context: Context) {
    /**
     * Share text content with other apps using Android's share intent
     */
    actual fun shareText(text: String, title: String?) {
        val sendIntent = Intent().apply {
            action = Intent.ACTION_SEND
            putExtra(Intent.EXTRA_TEXT, text)
            type = "text/plain"
        }
        
        val shareIntent = Intent.createChooser(sendIntent, title)
        shareIntent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        context.startActivity(shareIntent)
    }
    
    /**
     * Share the current page information
     */
    actual fun sharePage(pageTitle: String, pageDescription: String?) {
        val shareText = buildString {
            append(pageTitle)
            if (pageDescription != null) {
                append("\n\n")
                append(pageDescription)
            }
        }
        shareText(shareText, "分享")
    }
}
