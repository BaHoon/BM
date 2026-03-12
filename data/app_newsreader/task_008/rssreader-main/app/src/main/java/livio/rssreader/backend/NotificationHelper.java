package livio.rssreader.backend;
/*
IMPORTANT NOTICE, please read:

This software is licensed under the terms of the GNU GENERAL PUBLIC LICENSE,
please read the enclosed file license.txt or https://www.gnu.org/licenses/old-licenses/gpl-2.0-standalone.html

Note that this software is freeware and it is not designed, licensed or intended
for use in mission critical, life support and military purposes.

The use of this software is at the risk of the user.
*/

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.os.Build;

import androidx.core.app.NotificationCompat;
import androidx.core.app.NotificationManagerCompat;

import livio.rssreader.R;
import livio.rssreader.RSSReader;

/**
 * 通知助手类，管理应用内的所有通知功能
 */
public final class NotificationHelper {
    
    private static final String CHANNEL_ID = "rss_reader_channel";
    private static final String CHANNEL_NAME = "RSS Reader Notifications";
    private static final int NOTIFICATION_ID_UPDATE = 1;
    private static final int NOTIFICATION_ID_ERROR = 2;
    
    /**
     * 创建通知渠道（Android 8.0 及以上需要）
     */
    public static void createNotificationChannel(Context context) {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            NotificationChannel channel = new NotificationChannel(
                    CHANNEL_ID,
                    CHANNEL_NAME,
                    NotificationManager.IMPORTANCE_DEFAULT
            );
            channel.setDescription("RSS阅读器新闻更新通知");
            
            NotificationManager notificationManager = context.getSystemService(NotificationManager.class);
            if (notificationManager != null) {
                notificationManager.createNotificationChannel(channel);
            }
        }
    }
    
    /**
     * 检查通知是否已启用
     */
    public static boolean isNotificationEnabled(Context context) {
        SharedPreferences prefs = androidx.preference.PreferenceManager.getDefaultSharedPreferences(context);
        return prefs.getBoolean(RSSReader.PREF_ENABLE_NOTIFICATIONS, true);
    }
    
    /**
     * 检查系统层面通知权限是否已授予
     */
    public static boolean hasNotificationPermission(Context context) {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            NotificationManagerCompat notificationManager = NotificationManagerCompat.from(context);
            return notificationManager.areNotificationsEnabled();
        }
        return true; // Android 13 以下默认有权限
    }
    
    /**
     * 显示新闻更新通知
     */
    public static void showUpdateNotification(Context context, String title, String content) {
        if (!isNotificationEnabled(context)) {
            return;
        }
        
        if (!hasNotificationPermission(context)) {
            return;
        }
        
        createNotificationChannel(context);
        
        Intent intent = new Intent(context, RSSReader.class);
        intent.setFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_CLEAR_TASK);
        PendingIntent pendingIntent = PendingIntent.getActivity(
                context, 
                0, 
                intent, 
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE
        );
        
        NotificationCompat.Builder builder = new NotificationCompat.Builder(context, CHANNEL_ID)
                .setSmallIcon(R.mipmap.ic_launcher)
                .setContentTitle(title)
                .setContentText(content)
                .setPriority(NotificationCompat.PRIORITY_DEFAULT)
                .setContentIntent(pendingIntent)
                .setAutoCancel(true);
        
        NotificationManagerCompat notificationManager = NotificationManagerCompat.from(context);
        try {
            notificationManager.notify(NOTIFICATION_ID_UPDATE, builder.build());
        } catch (SecurityException e) {
            // 通知权限被拒绝，静默处理
        }
    }
    
    /**
     * 显示错误通知
     */
    public static void showErrorNotification(Context context, String title, String content) {
        if (!isNotificationEnabled(context)) {
            return;
        }
        
        if (!hasNotificationPermission(context)) {
            return;
        }
        
        createNotificationChannel(context);
        
        Intent intent = new Intent(context, RSSReader.class);
        intent.setFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_CLEAR_TASK);
        PendingIntent pendingIntent = PendingIntent.getActivity(
                context, 
                0, 
                intent, 
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE
        );
        
        NotificationCompat.Builder builder = new NotificationCompat.Builder(context, CHANNEL_ID)
                .setSmallIcon(R.mipmap.ic_launcher)
                .setContentTitle(title)
                .setContentText(content)
                .setPriority(NotificationCompat.PRIORITY_DEFAULT)
                .setContentIntent(pendingIntent)
                .setAutoCancel(true);
        
        NotificationManagerCompat notificationManager = NotificationManagerCompat.from(context);
        try {
            notificationManager.notify(NOTIFICATION_ID_ERROR, builder.build());
        } catch (SecurityException e) {
            // 通知权限被拒绝，静默处理
        }
    }
    
    /**
     * 取消所有通知
     */
    public static void cancelAllNotifications(Context context) {
        NotificationManagerCompat notificationManager = NotificationManagerCompat.from(context);
        notificationManager.cancelAll();
    }
}
