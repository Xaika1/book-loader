package ru.uchebniki.sync;

import android.Manifest;
import android.app.Activity;
import android.app.AlertDialog;
import android.content.ActivityNotFoundException;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.content.res.Configuration;
import android.graphics.Color;
import android.graphics.Typeface;
import android.graphics.drawable.GradientDrawable;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.os.PowerManager;
import android.provider.Settings;
import android.text.InputType;
import android.text.TextUtils;
import android.util.TypedValue;
import android.view.Gravity;
import android.view.View;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.ProgressBar;
import android.widget.ScrollView;
import android.widget.TextView;

import java.text.DateFormat;
import java.util.Date;

/** Экран приложения: имя планшета, что происходит, кнопки разрешений. */
public class MainActivity extends Activity {
    private static final int REQ_STORAGE = 1, REQ_NOTIFY = 2;

    private final Handler handler = new Handler(Looper.getMainLooper());
    private final Runnable ticker = new Runnable() {
        @Override
        public void run() {
            render();
            handler.postDelayed(this, 700);
        }
    };

    private boolean dark;
    private int fg, muted, accent, card, errColor;
    private TextView nameView, statusView, detailView, booksView;
    private ProgressBar progress;
    private LinearLayout permBox, batteryBox;

    @Override
    protected void onCreate(Bundle saved) {
        super.onCreate(saved);
        dark = (getResources().getConfiguration().uiMode & Configuration.UI_MODE_NIGHT_MASK) == Configuration.UI_MODE_NIGHT_YES;
        fg = dark ? 0xFFECEBE7 : 0xFF1D1D1B;
        muted = dark ? 0xFFA3A29C : 0xFF6B6A65;
        accent = dark ? 0xFF6CC296 : 0xFF2F6F4F;
        card = dark ? 0xFF222325 : 0xFFF3F2EE;
        errColor = dark ? 0xFFF2867D : 0xFFB3261E;
        buildUi();
        if (Build.VERSION.SDK_INT >= 33 && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED
                && !SyncService.prefs(this).getBoolean("asked_notify", false)) {
            SyncService.prefs(this).edit().putBoolean("asked_notify", true).apply();
            requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS}, REQ_NOTIFY);
        }
    }

    @Override
    protected void onResume() {
        super.onResume();
        SyncService.start(this);
        handler.post(ticker);
    }

    @Override
    protected void onPause() {
        handler.removeCallbacks(ticker);
        super.onPause();
    }

    private void buildUi() {
        LinearLayout col = new LinearLayout(this);
        col.setOrientation(LinearLayout.VERTICAL);
        int pad = dp(24);
        col.setPadding(pad, dp(32), pad, pad);

        col.addView(text("УЧЕБНИКИ · ЭТОТ ПЛАНШЕТ", 13, muted, true));
        nameView = text("…", 36, fg, true);
        col.addView(nameView);

        statusView = text("", 20, fg, false);
        statusView.setPadding(0, dp(16), 0, dp(8));
        col.addView(statusView);
        progress = new ProgressBar(this, null, android.R.attr.progressBarStyleHorizontal);
        progress.setMax(1000);
        col.addView(progress, new LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, dp(12)));
        detailView = text("", 14, muted, false);
        detailView.setPadding(0, dp(8), 0, 0);
        col.addView(detailView);

        permBox = box("Приложению нужен доступ к файлам, чтобы класть учебники в папку Books.",
                "Разрешить доступ к файлам", v -> askStorage());
        col.addView(permBox);
        batteryBox = box("Разрешите работу в фоне, чтобы учебники скачивались, даже когда приложение закрыто.",
                "Разрешить работу в фоне", v -> askBattery());
        col.addView(batteryBox);

        LinearLayout buttons = new LinearLayout(this);
        buttons.setOrientation(LinearLayout.HORIZONTAL);
        buttons.setPadding(0, dp(20), 0, 0);
        Button now = button("Проверить сейчас", true);
        now.setOnClickListener(v -> {
            SyncService.syncNow(this);
            statusView.setText("Проверяю…");
        });
        Button addr = button("Адрес компьютера", false);
        addr.setOnClickListener(v -> askAddress());
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1);
        lp.setMargins(0, 0, dp(8), 0);
        buttons.addView(now, lp);
        buttons.addView(addr, new LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1));
        col.addView(buttons);

        TextView where = text("Учебники лежат во внутренней памяти в папке «Books». Откройте их в любой читалке.", 14, muted, false);
        where.setPadding(0, dp(24), 0, dp(8));
        col.addView(where);
        booksView = text("", 15, fg, false);
        booksView.setLineSpacing(dp(4), 1f);
        col.addView(booksView);

        ScrollView scroll = new ScrollView(this);
        scroll.addView(col);
        setContentView(scroll);
    }

    private void render() {
        SyncState s = SyncState.snapshot();
        nameView.setText(TextUtils.isEmpty(s.name) ? "Ещё не подключён" : s.name);
        statusView.setText(s.message);
        boolean bad = SyncState.ERROR.equals(s.state) || SyncState.NO_PERMISSION.equals(s.state) || SyncState.SEARCHING.equals(s.state);
        statusView.setTextColor(bad ? errColor : SyncState.OK.equals(s.state) ? accent : fg);

        boolean syncing = SyncState.SYNCING.equals(s.state) && s.total > 0;
        progress.setVisibility(syncing ? View.VISIBLE : View.GONE);
        if (syncing) {
            double file = s.fileSize > 0 && !s.current.isEmpty() ? Math.min(1.0, (double) s.fileBytes / s.fileSize) : 0;
            progress.setProgress((int) (1000 * (s.done + file) / s.total));
        }

        StringBuilder d = new StringBuilder();
        if (syncing && !s.current.isEmpty()) {
            d.append(s.current);
            if (s.fileSize > 0) d.append(" — ").append(Syncer.human(s.fileBytes)).append(" из ").append(Syncer.human(s.fileSize));
            d.append('\n');
        }
        d.append("Компьютер: ").append(s.server.isEmpty() ? "не найден" : s.server.replace("http://", ""));
        if (s.lastCheck > 0) d.append("\nПоследняя проверка: ").append(DateFormat.getTimeInstance(DateFormat.SHORT).format(new Date(s.lastCheck)));
        d.append("\nВерсия приложения: ").append(BuildInfo.versionName(this));
        detailView.setText(d);

        permBox.setVisibility(Storage.canWrite(this) ? View.GONE : View.VISIBLE);
        boolean batteryOk = true;
        if (Build.VERSION.SDK_INT >= 23) {
            PowerManager pm = (PowerManager) getSystemService(POWER_SERVICE);
            batteryOk = pm.isIgnoringBatteryOptimizations(getPackageName());
        }
        batteryBox.setVisibility(batteryOk ? View.GONE : View.VISIBLE);

        if (s.books.isEmpty()) {
            booksView.setText(SyncState.OK.equals(s.state) ? "Учебников пока нет." : "");
        } else {
            StringBuilder b = new StringBuilder("Учебники (").append(s.books.size()).append("):\n");
            for (String p : s.books) b.append("• ").append(p).append('\n');
            booksView.setText(b);
        }
    }

    // ------------------------------------------------------------ разрешения

    private void askStorage() {
        if (Build.VERSION.SDK_INT >= 30) {
            try {
                startActivity(new Intent(Settings.ACTION_MANAGE_APP_ALL_FILES_ACCESS_PERMISSION, Uri.parse("package:" + getPackageName())));
            } catch (ActivityNotFoundException e) {
                startActivity(new Intent(Settings.ACTION_MANAGE_ALL_FILES_ACCESS_PERMISSION));
            }
        } else if (Build.VERSION.SDK_INT >= 23) {
            if (SyncService.prefs(this).getBoolean("asked_storage", false)
                    && !shouldShowRequestPermissionRationale(Manifest.permission.WRITE_EXTERNAL_STORAGE)) {
                // уже отказали с «больше не спрашивать» — открываем настройки приложения
                startActivity(new Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS, Uri.parse("package:" + getPackageName())));
                return;
            }
            SyncService.prefs(this).edit().putBoolean("asked_storage", true).apply();
            requestPermissions(new String[]{Manifest.permission.WRITE_EXTERNAL_STORAGE}, REQ_STORAGE);
        }
    }

    private void askBattery() {
        if (Build.VERSION.SDK_INT < 23) return;
        try {
            startActivity(new Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS, Uri.parse("package:" + getPackageName())));
        } catch (ActivityNotFoundException e) {
            startActivity(new Intent(Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS));
        }
    }

    @Override
    public void onRequestPermissionsResult(int code, String[] perms, int[] results) {
        SyncService.syncNow(this);
    }

    private void askAddress() {
        SharedPreferences p = SyncService.prefs(this);
        EditText input = new EditText(this);
        input.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_URI);
        input.setHint("например 192.168.1.10");
        input.setText(p.getString(Syncer.PREF_MANUAL, ""));
        LinearLayout wrap = new LinearLayout(this);
        wrap.setPadding(dp(20), dp(8), dp(20), 0);
        wrap.addView(input, new LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT));
        new AlertDialog.Builder(this)
                .setTitle("Адрес компьютера")
                .setMessage("Обычно указывать не нужно — приложение находит компьютер само. "
                        + "Если не находит, введите адрес, который показан в программе на компьютере.")
                .setView(wrap)
                .setPositiveButton("Сохранить", (dlg, w) -> {
                    String a = input.getText().toString().trim();
                    p.edit().putString(Syncer.PREF_MANUAL, a).remove(Syncer.PREF_SERVER).apply();
                    SyncService.syncNow(this);
                })
                .setNeutralButton("Искать сам", (dlg, w) -> {
                    p.edit().remove(Syncer.PREF_MANUAL).remove(Syncer.PREF_SERVER).apply();
                    SyncService.syncNow(this);
                })
                .setNegativeButton("Отмена", null)
                .show();
    }

    // ------------------------------------------------------------ вёрстка

    private int dp(int v) {
        return (int) TypedValue.applyDimension(TypedValue.COMPLEX_UNIT_DIP, v, getResources().getDisplayMetrics());
    }

    private TextView text(String s, int sp, int color, boolean bold) {
        TextView t = new TextView(this);
        t.setText(s);
        t.setTextSize(TypedValue.COMPLEX_UNIT_SP, sp);
        t.setTextColor(color);
        if (bold) t.setTypeface(Typeface.DEFAULT_BOLD);
        return t;
    }

    private Button button(String label, boolean primary) {
        Button b = new Button(this);
        b.setText(label);
        b.setAllCaps(false);
        b.setTextSize(TypedValue.COMPLEX_UNIT_SP, 16);
        GradientDrawable bg = new GradientDrawable();
        bg.setCornerRadius(dp(10));
        if (primary) {
            bg.setColor(accent);
            b.setTextColor(dark ? 0xFF0F2419 : Color.WHITE);
        } else {
            bg.setColor(card);
            bg.setStroke(dp(1), muted);
            b.setTextColor(fg);
        }
        b.setBackground(bg);
        b.setPadding(dp(12), dp(14), dp(12), dp(14));
        return b;
    }

    private LinearLayout box(String message, String action, View.OnClickListener onClick) {
        LinearLayout box = new LinearLayout(this);
        box.setOrientation(LinearLayout.VERTICAL);
        box.setPadding(dp(16), dp(14), dp(16), dp(14));
        GradientDrawable bg = new GradientDrawable();
        bg.setCornerRadius(dp(12));
        bg.setColor(card);
        bg.setStroke(dp(2), errColor);
        box.setBackground(bg);
        TextView t = text(message, 16, fg, false);
        box.addView(t);
        Button b = button(action, true);
        b.setOnClickListener(onClick);
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(LinearLayout.LayoutParams.WRAP_CONTENT, LinearLayout.LayoutParams.WRAP_CONTENT);
        lp.topMargin = dp(10);
        box.addView(b, lp);
        LinearLayout.LayoutParams outer = new LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT);
        outer.topMargin = dp(16);
        box.setLayoutParams(outer);
        box.setGravity(Gravity.START);
        return box;
    }
}
