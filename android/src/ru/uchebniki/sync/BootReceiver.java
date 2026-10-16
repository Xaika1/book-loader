package ru.uchebniki.sync;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;

/** Запускает скачивание учебников после включения планшета и после обновления приложения. */
public class BootReceiver extends BroadcastReceiver {
    @Override
    public void onReceive(Context context, Intent intent) {
        SyncService.start(context);
    }
}
