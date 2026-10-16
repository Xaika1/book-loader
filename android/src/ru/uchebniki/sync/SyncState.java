package ru.uchebniki.sync;

import java.util.ArrayList;
import java.util.List;

/** Что сейчас происходит с синхронизацией. Пишет фоновый поток, читает экран приложения. */
final class SyncState {
    static final String STARTING = "starting";
    static final String SEARCHING = "searching";
    static final String SYNCING = "syncing";
    static final String OK = "ok";
    static final String ERROR = "error";
    static final String NO_PERMISSION = "no_permission";

    String state = STARTING;
    String message = "Запуск…";
    String error = "";
    String server = "";
    String name = "";
    String version = "";
    String current = "";
    int done, total;
    long fileBytes, fileSize;
    long lastCheck;
    List<String> books = new ArrayList<>();

    private static final SyncState INSTANCE = new SyncState();

    interface Change {
        void apply(SyncState s);
    }

    static void update(Change change) {
        synchronized (INSTANCE) {
            change.apply(INSTANCE);
        }
    }

    static SyncState snapshot() {
        synchronized (INSTANCE) {
            SyncState c = new SyncState();
            c.state = INSTANCE.state;
            c.message = INSTANCE.message;
            c.error = INSTANCE.error;
            c.server = INSTANCE.server;
            c.name = INSTANCE.name;
            c.version = INSTANCE.version;
            c.current = INSTANCE.current;
            c.done = INSTANCE.done;
            c.total = INSTANCE.total;
            c.fileBytes = INSTANCE.fileBytes;
            c.fileSize = INSTANCE.fileSize;
            c.lastCheck = INSTANCE.lastCheck;
            c.books = new ArrayList<>(INSTANCE.books);
            return c;
        }
    }
}
