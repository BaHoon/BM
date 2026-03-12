package com.maksimowiczm.foodyou.common.share

/**
 * Platform-specific share functionality manager
 */
expect class ShareManager {
    /**
     * Share text content with other apps
     * @param text The text content to share
     * @param title The title of the share dialog (optional)
     */
    fun shareText(text: String, title: String? = null)
    
    /**
     * Share the current page/screen information
     * @param pageTitle The title of the current page
     * @param pageDescription Optional description of the page
     */
    fun sharePage(pageTitle: String, pageDescription: String? = null)
}
