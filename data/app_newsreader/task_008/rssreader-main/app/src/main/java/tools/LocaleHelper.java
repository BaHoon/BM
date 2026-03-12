package tools;

import android.content.Context;
import android.content.SharedPreferences;
import android.content.res.Configuration;
import android.content.res.Resources;
import android.os.Build;
import android.os.LocaleList;

import androidx.preference.PreferenceManager;

import java.util.Locale;

public class LocaleHelper {

    private static final String PREF_APP_LANGUAGE = "app_language";

    public static Context setLocale(Context context) {
        SharedPreferences preferences = PreferenceManager.getDefaultSharedPreferences(context);
        String language = preferences.getString(PREF_APP_LANGUAGE, "");
        
        if (language.isEmpty()) {
            // Use system default language
            return context;
        }
        
        return updateResources(context, language);
    }

    public static Context setLocale(Context context, String language) {
        SharedPreferences preferences = PreferenceManager.getDefaultSharedPreferences(context);
        SharedPreferences.Editor editor = preferences.edit();
        editor.putString(PREF_APP_LANGUAGE, language);
        editor.apply();
        
        return updateResources(context, language);
    }

    private static Context updateResources(Context context, String language) {
        Locale locale = new Locale(language);
        Locale.setDefault(locale);

        Resources resources = context.getResources();
        Configuration configuration = resources.getConfiguration();

        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.N) {
            LocaleList localeList = new LocaleList(locale);
            LocaleList.setDefault(localeList);
            configuration.setLocales(localeList);
        } else {
            configuration.setLocale(locale);
        }

        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.N_MR1) {
            return context.createConfigurationContext(configuration);
        } else {
            resources.updateConfiguration(configuration, resources.getDisplayMetrics());
            return context;
        }
    }

    public static String getPersistedLocale(Context context) {
        SharedPreferences preferences = PreferenceManager.getDefaultSharedPreferences(context);
        return preferences.getString(PREF_APP_LANGUAGE, "");
    }
}
