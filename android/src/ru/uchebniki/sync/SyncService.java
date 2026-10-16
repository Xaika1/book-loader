package ru.uchebniki.sync;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.net.wifi.WifiManager;
import android.os.Build;
import android.os.IBinder;
import android.os.PowerManager;

/** Постоянно работающая служба: раз в полминуты сверяет учебники с компьютером. */
public class SyncService extends Service {
    private static final String CHANNEL = "sync";
    private static final int NOTIFICATION_ID = 1;
    private static volatile SyncService instance;

    private final Object wake = new Object();
    private volatile boolean stopping;
    private boolean woken;
    private Thread worker;
    private PowerManager.WakeLock wakeLock;
    private WifiManager.WifiLock wifiLock;
    private String shownText = "";

    static SharedPreferences prefs(Context ctx) {
        return ctx.getSharedPreferences("uchebniki", MODE_PRIVATE);
    }

    static void start(Context ctx) {
        Intent i = new Intent(ctx, SyncService.class);
        if (Build.VERSION.SDK_INT >= 26) ctx.startForegroundService(i);
        else ctx.startService(i);
    }

    /** «Проверить сейчас» — прервать ожидание. */
    static void syncNow(Context ctx) {
        SyncService s = instance;
        if (s != null) s.wakeUp();
        else start(ctx);
    }

    @Override
    public void onCreate() {
        super.onCreate();
        instance = this;
        String name = prefs(this).getString(Syncer.PREF_NAME, "");
        SyncState.update(s -> s.name = name);
        if (Build.VERSION.SDK_INT >= 26) {
            NotificationChannel ch = new NotificationChannel(CHANNEL, "Скачивание учебников", NotificationManager.IMPORTANCE_LOW);
            ch.setShowBadge(false);
            getSystemService(NotificationManager.class).createNotificationChannel(ch);
        }
        startForeground(NOTIFICATION_ID, buildNotification("Запуск…"));
        PowerManager pm = (PowerManager) getSystemService(POWER_SERVICE);
        wakeLock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "uchebniki:download");
        WifiManager wm = (WifiManager) getApplicationContext().getSystemService(WIFI_SERVICE);
        if (wm != null) wifiLock = wm.createWifiLock(WifiManager.WIFI_MODE_FULL_HIGH_PERF, "uchebniki:download");
        worker = new Thread(this::loop, "uchebniki-sync");
        worker.start();
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        wakeUp();
        return START_STICKY;
    }

    @Override
    public void onDestroy() {
        stopping = true;
        instance = null;
        if (worker != null) worker.interrupt();
        holdLocks(false);
        super.onDestroy();
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    private void loop() {
        Syncer syncer = new Syncer(this);
        while (!stopping) {
            long wait;
            try {
                wait = syncer.runOnce();
            } catch (InterruptedException e) {
                return;
            } catch (Throwable t) {
                String msg = String.valueOf(t.getMessage());
                SyncState.update(s -> {
                    s.state = SyncState.ERROR;
                    s.message = "Ошибка: " + msg;
                    s.error = msg;
                });
                wait = 30000;
            }
            refreshNotification();
            try {
                sleepUnlessWoken(wait);
            } catch (InterruptedException e) {
                return;
            }
        }
    }

    boolean isStopping() {
        return stopping;
    }

    void sleepUnlessWoken(long ms) throws InterruptedException {
        synchronized (wake) {
            long until = System.currentTimeMillis() + ms;
            while (!woken && !stopping) {
                long left = until - System.currentTimeMillis();
                if (left <= 0) break;
                wake.wait(left);
            }
            woken = false;
        }
        if (stopping) throw new InterruptedException();
    }

    private void wakeUp() {
        synchronized (wake) {
            woken = true;
            wake.notifyAll();
        }
    }

    /** Не даём планшету уснуть и выключить Wi-Fi, пока идёт скачивание. */
    void holdLocks(boolean hold) {
        try {
            if (hold) {
                if (!wakeLock.isHeld()) wakeLock.acquire(60 * 60 * 1000L);
                if (wifiLock != null && !wifiLock.isHeld()) wifiLock.acquire();
            } else {
                if (wakeLock != null && wakeLock.isHeld()) wakeLock.release();
                if (wifiLock != null && wifiLock.isHeld()) wifiLock.release();
            }
        } catch (RuntimeException ignored) {
        }
    }

    synchronized void refreshNotification() {
        String text = SyncState.snapshot().message;
        if (text.equals(shownText)) return;
        shownText = text;
        NotificationManager nm = (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
        nm.notify(NOTIFICATION_ID, buildNotification(text));
    }

    private Notification buildNotification(String text) {
        Intent open = new Intent(this, MainActivity.class);
        int flags = Build.VERSION.SDK_INT >= 23 ? PendingIntent.FLAG_IMMUTABLE : 0;
        PendingIntent pi = PendingIntent.getActivity(this, 0, open, flags);
        Notification.Builder b = Build.VERSION.SDK_INT >= 26
                ? new Notification.Builder(this, CHANNEL)
                : new Notification.Builder(this).setPriority(Notification.PRIORITY_LOW);
        return b.setSmallIcon(R.drawable.ic_notify)
                .setContentTitle("Учебники")
                .setContentText(text)
                .setContentIntent(pi)
                .setOngoing(true)
                .setShowWhen(false)
                .build();
    }
}
