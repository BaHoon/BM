package livio.rssreader.backend;
/*
Version 1.0, 24-02-2026, Offline Manager for RSS Reader

IMPORTANT NOTICE, please read:

This software is licensed under the terms of the GNU GENERAL PUBLIC LICENSE,
please read the enclosed file license.txt or https://www.gnu.org/licenses/old-licenses/gpl-2.0-standalone.html

Note that this software is freeware and it is not designed, licensed or intended
for use in mission critical, life support and military purposes.

The use of this software is at the risk of the user.
*/

import android.content.Context;
import android.os.Build;
import android.os.Environment;
import android.text.Html;
import android.util.Log;

import java.io.BufferedInputStream;
import java.io.File;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.text.SimpleDateFormat;
import java.util.Date;
import java.util.Locale;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * Manager class for handling offline saving of RSS articles
 */
public class OfflineManager {
    private static final String TAG = "OfflineManager";
    private static final String OFFLINE_DIR = "RSSReader_Offline";
    private static final int BUFFER_SIZE = 8192;
    private static final int CONNECT_TIMEOUT = 10000; // 10 seconds
    private static final int READ_TIMEOUT = 15000; // 15 seconds

    private final Context context;

    public OfflineManager(Context context) {
        this.context = context;
    }

    /**
     * Save an article offline to SD card
     *
     * @param item    The RSS item to save
     * @param content The HTML content of the article
     * @return true if saved successfully, false otherwise
     */
    public boolean saveArticleOffline(RSSItem item, String content) {
        if (item == null || content == null || content.isEmpty()) {
            Log.e(TAG, "Invalid article or content");
            return false;
        }

        File offlineDir = getOfflineStorageDir();
        if (offlineDir == null) {
            Log.e(TAG, "Cannot access offline storage directory");
            return false;
        }

        String articleFileName = generateArticleFileName(item);
        File articleFile = new File(offlineDir, articleFileName);

        // Check if already saved
        if (articleFile.exists()) {
            Log.i(TAG, "Article already saved: " + articleFileName);
            return true;
        }

        try {
            // Generate full HTML with styles and embedded resources
            String fullHtml = generateOfflineHtml(item, content);

            // Save the HTML file
            try (FileOutputStream fos = new FileOutputStream(articleFile)) {
                fos.write(fullHtml.getBytes(StandardCharsets.UTF_8));
            }

            // Download and save images
            downloadImages(content, offlineDir, articleFileName);

            Log.i(TAG, "Article saved successfully: " + articleFileName);
            return true;

        } catch (IOException e) {
            Log.e(TAG, "Error saving article offline", e);
            // Clean up partial files
            if (articleFile.exists()) {
                articleFile.delete();
            }
            return false;
        }
    }

    /**
     * Check if an article is already saved offline
     *
     * @param item The RSS item to check
     * @return true if already saved, false otherwise
     */
    public boolean isArticleSavedOffline(RSSItem item) {
        File offlineDir = getOfflineStorageDir();
        if (offlineDir == null) {
            return false;
        }

        String articleFileName = generateArticleFileName(item);
        File articleFile = new File(offlineDir, articleFileName);
        return articleFile.exists();
    }

    /**
     * Get the offline storage directory
     *
     * @return The directory for offline storage, or null if not available
     */
    private File getOfflineStorageDir() {
        File storageDir;
        
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            // Android 10+ use app-specific directory
            storageDir = new File(context.getExternalFilesDir(null), OFFLINE_DIR);
        } else {
            // For older versions, use public external storage
            if (!Environment.MEDIA_MOUNTED.equals(Environment.getExternalStorageState())) {
                Log.e(TAG, "External storage not mounted");
                return null;
            }
            storageDir = new File(Environment.getExternalStorageDirectory(), OFFLINE_DIR);
        }

        // Create directory if it doesn't exist
        if (!storageDir.exists()) {
            if (!storageDir.mkdirs()) {
                Log.e(TAG, "Failed to create offline storage directory");
                return null;
            }
        }

        return storageDir;
    }

    /**
     * Generate a unique filename for an article
     *
     * @param item The RSS item
     * @return A unique filename based on the article title and link
     */
    private String generateArticleFileName(RSSItem item) {
        String title = item.getTitle(false);
        String link = item.getLink();

        // Create a unique identifier from title and link
        String identifier = title + link;
        String hash = generateHash(identifier);

        // Sanitize title for filename
        String sanitizedTitle = sanitizeFileName(title);
        if (sanitizedTitle.length() > 50) {
            sanitizedTitle = sanitizedTitle.substring(0, 50);
        }

        return sanitizedTitle + "_" + hash + ".html";
    }

    /**
     * Generate MD5 hash of a string
     *
     * @param input The input string
     * @return The MD5 hash
     */
    private String generateHash(String input) {
        try {
            MessageDigest md = MessageDigest.getInstance("MD5");
            byte[] digest = md.digest(input.getBytes(StandardCharsets.UTF_8));
            StringBuilder sb = new StringBuilder();
            for (byte b : digest) {
                sb.append(String.format("%02x", b));
            }
            return sb.toString().substring(0, 8); // Use first 8 characters
        } catch (NoSuchAlgorithmException e) {
            Log.e(TAG, "Error generating hash", e);
            return String.valueOf(input.hashCode());
        }
    }

    /**
     * Sanitize a filename by removing invalid characters
     *
     * @param name The filename to sanitize
     * @return The sanitized filename
     */
    private String sanitizeFileName(String name) {
        // Remove HTML tags
        String plainText = Html.fromHtml(name).toString();
        // Replace invalid filename characters with underscore
        return plainText.replaceAll("[^a-zA-Z0-9\\s\\-_]", "_").replaceAll("\\s+", "_");
    }

    /**
     * Generate complete HTML for offline viewing
     *
     * @param item    The RSS item
     * @param content The article content
     * @return Complete HTML string
     */
    private String generateOfflineHtml(RSSItem item, String content) {
        SimpleDateFormat dateFormat = new SimpleDateFormat("yyyy-MM-dd HH:mm", Locale.US);
        String pubDate = item.getPubDate(context);
        String title = item.getTitle(false);
        String link = item.getLink();

        StringBuilder html = new StringBuilder();
        html.append("<!DOCTYPE html>\n");
        html.append("<html>\n<head>\n");
        html.append("<meta charset=\"UTF-8\">\n");
        html.append("<meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\">\n");
        html.append("<title>").append(Html.escapeHtml(title)).append("</title>\n");
        html.append("<style>\n");
        html.append("body { font-family: sans-serif; margin: 16px; line-height: 1.6; }\n");
        html.append("h1 { font-size: 24px; margin-top: 0; }\n");
        html.append(".meta { color: #666; font-size: 14px; margin-bottom: 16px; }\n");
        html.append(".link { margin-bottom: 16px; }\n");
        html.append(".content { font-size: 16px; }\n");
        html.append("img { max-width: 100%; height: auto; }\n");
        html.append("a { color: #0066cc; text-decoration: none; }\n");
        html.append(".offline-notice { background-color: #fff3cd; padding: 10px; margin-bottom: 16px; border-radius: 5px; }\n");
        html.append("</style>\n");
        html.append("</head>\n<body>\n");
        
        html.append("<div class=\"offline-notice\">Saved for offline reading on ")
            .append(dateFormat.format(new Date())).append("</div>\n");
        
        html.append("<h1>").append(title).append("</h1>\n");
        html.append("<div class=\"meta\">Published: ").append(pubDate).append("</div>\n");
        
        if (!link.isEmpty()) {
            html.append("<div class=\"link\"><a href=\"").append(link)
                .append("\">View original article</a></div>\n");
        }
        
        html.append("<div class=\"content\">\n");
        html.append(content);
        html.append("\n</div>\n");
        html.append("</body>\n</html>");

        return html.toString();
    }

    /**
     * Download and save images from the HTML content
     *
     * @param content         The HTML content
     * @param offlineDir      The offline storage directory
     * @param articleFileName The article filename (used for creating image subdirectory)
     */
    private void downloadImages(String content, File offlineDir, String articleFileName) {
        // Create images subdirectory
        String imagesDirName = articleFileName.replace(".html", "_images");
        File imagesDir = new File(offlineDir, imagesDirName);
        
        if (!imagesDir.exists()) {
            if (!imagesDir.mkdirs()) {
                Log.w(TAG, "Failed to create images directory");
                return;
            }
        }

        // Find all image URLs in the content
        Pattern imgPattern = Pattern.compile("<img[^>]+src=\"([^\"]+)\"", Pattern.CASE_INSENSITIVE);
        Matcher matcher = imgPattern.matcher(content);

        int imageCount = 0;
        while (matcher.find() && imageCount < 50) { // Limit to 50 images
            String imageUrl = matcher.group(1);
            if (imageUrl != null && (imageUrl.startsWith("http://") || imageUrl.startsWith("https://"))) {
                try {
                    downloadImage(imageUrl, imagesDir);
                    imageCount++;
                } catch (IOException e) {
                    Log.w(TAG, "Failed to download image: " + imageUrl, e);
                    // Continue with next image
                }
            }
        }

        Log.i(TAG, "Downloaded " + imageCount + " images");
    }

    /**
     * Download a single image
     *
     * @param imageUrl  The URL of the image
     * @param targetDir The directory to save the image
     * @throws IOException If download fails
     */
    private void downloadImage(String imageUrl, File targetDir) throws IOException {
        URL url = new URL(imageUrl);
        String fileName = generateImageFileName(imageUrl);
        File targetFile = new File(targetDir, fileName);

        // Skip if already downloaded
        if (targetFile.exists()) {
            return;
        }

        HttpURLConnection connection = null;
        try {
            connection = (HttpURLConnection) url.openConnection();
            connection.setConnectTimeout(CONNECT_TIMEOUT);
            connection.setReadTimeout(READ_TIMEOUT);
            connection.setRequestProperty("User-Agent", "Mozilla/5.0");

            int responseCode = connection.getResponseCode();
            if (responseCode != HttpURLConnection.HTTP_OK) {
                throw new IOException("HTTP response code: " + responseCode);
            }

            try (InputStream input = new BufferedInputStream(connection.getInputStream());
                 OutputStream output = new FileOutputStream(targetFile)) {

                byte[] buffer = new byte[BUFFER_SIZE];
                int bytesRead;
                long totalBytes = 0;
                long maxSize = 10 * 1024 * 1024; // 10MB limit per image

                while ((bytesRead = input.read(buffer)) != -1 && totalBytes < maxSize) {
                    output.write(buffer, 0, bytesRead);
                    totalBytes += bytesRead;
                }

                if (totalBytes >= maxSize) {
                    Log.w(TAG, "Image too large, truncated: " + imageUrl);
                }
            }
        } finally {
            if (connection != null) {
                connection.disconnect();
            }
        }
    }

    /**
     * Generate a filename for an image from its URL
     *
     * @param imageUrl The image URL
     * @return A sanitized filename
     */
    private String generateImageFileName(String imageUrl) {
        String fileName = imageUrl.substring(imageUrl.lastIndexOf('/') + 1);
        
        // Remove query parameters
        int queryIndex = fileName.indexOf('?');
        if (queryIndex > 0) {
            fileName = fileName.substring(0, queryIndex);
        }

        // Ensure valid extension
        if (!fileName.contains(".")) {
            fileName += ".jpg";
        }

        return sanitizeFileName(fileName);
    }

    /**
     * Get the path to the offline storage directory
     *
     * @return The absolute path, or null if not available
     */
    public String getOfflineStoragePath() {
        File dir = getOfflineStorageDir();
        return dir != null ? dir.getAbsolutePath() : null;
    }

    /**
     * Delete an offline article
     *
     * @param item The RSS item to delete
     * @return true if deleted successfully, false otherwise
     */
    public boolean deleteOfflineArticle(RSSItem item) {
        File offlineDir = getOfflineStorageDir();
        if (offlineDir == null) {
            return false;
        }

        String articleFileName = generateArticleFileName(item);
        File articleFile = new File(offlineDir, articleFileName);

        if (articleFile.exists()) {
            return articleFile.delete();
        }

        return false;
    }

    /**
     * Get the total size of offline storage in bytes
     *
     * @return The total size in bytes
     */
    public long getOfflineStorageSize() {
        File offlineDir = getOfflineStorageDir();
        if (offlineDir == null) {
            return 0;
        }

        return calculateDirectorySize(offlineDir);
    }

    /**
     * Calculate the size of a directory recursively
     *
     * @param directory The directory to calculate
     * @return The total size in bytes
     */
    private long calculateDirectorySize(File directory) {
        long size = 0;
        if (directory.exists() && directory.isDirectory()) {
            File[] files = directory.listFiles();
            if (files != null) {
                for (File file : files) {
                    if (file.isFile()) {
                        size += file.length();
                    } else if (file.isDirectory()) {
                        size += calculateDirectorySize(file);
                    }
                }
            }
        }
        return size;
    }
}
