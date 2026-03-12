package com.maksimowiczm.foodyou.common.share

/**
 * iOS stub implementation of ShareManager
 * TODO: Implement actual iOS sharing functionality when iOS support is added
 */
actual class ShareManager {
    actual fun shareText(text: String, title: String?) {
        // iOS implementation will be added when iOS support is enabled
        println("Share on iOS: $text")
    }
    
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
