package ru.uchebniki.sync;

import android.content.Context;
import android.content.pm.PackageManager;

final class BuildInfo {
    static String versionName(Context ctx) {
        try {
            return ctx.getPackageManager().getPackageInfo(ctx.getPackageName(), 0).versionName;
        } catch (PackageManager.NameNotFoundException e) {
            return "?";
        }
    }
}
