package ru.uchebniki.sync;

import android.annotation.SuppressLint;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.content.SharedPreferences;
import android.media.MediaScannerConnection;
import android.net.DhcpInfo;
import android.net.wifi.WifiManager;
import android.os.BatteryManager;
import android.os.Build;
import android.provider.Settings;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.DatagramPacket;
import java.net.DatagramSocket;
import java.net.HttpURLConnection;
import java.net.InetAddress;
import java.net.SocketTimeoutException;
import java.net.URL;
import java.net.URLEncoder;
import java.util.ArrayList;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.TreeMap;
import java.util.UUID;

/** Одна проверка: найти компьютер, сравнить список учебников, скачать новое, удалить лишнее. */
final class Syncer {
    static final int DEFAULT_PORT = 8765;
    static final int DISCOVERY_PORT = 8766;
    static final String PREF_SERVER = "server";        // последний рабочий адрес
    static final String PREF_MANUAL = "manual_server"; // адрес, введённый вручную
    static final String PREF_NAME = "name";

    /** Сервер занят (много планшетов качают одновременно) — подождать и повторить. */
    static final class BusyException extends IOException {
        BusyException() {
            super("busy");
        }
    }

    private final Context ctx;
    private final SharedPreferences prefs;
    private final SyncService service;
    private long lastReport;
    private int interval = 30;

    Syncer(SyncService service) {
        this.service = service;
        this.ctx = service.getApplicationContext();
        this.prefs = SyncService.prefs(ctx);
    }

    /** Возвращает, сколько миллисекунд ждать до следующей проверки. */
    long runOnce() throws InterruptedException {
        SyncState.update(s -> s.lastCheck = System.currentTimeMillis());
        String server = findServer();
        if (server == null) {
            set(SyncState.SEARCHING, "Компьютер с учебниками не найден. Проверьте, что планшет подключён к школьному Wi-Fi.", "");
            return 20000;
        }
        SyncState.update(s -> s.server = server);
        if (!Storage.canWrite(ctx)) {
            set(SyncState.NO_PERMISSION, "Нужно разрешить доступ к файлам — откройте приложение «Учебники».", "");
            report(server, true);
            return 15000;
        }
        try {
            return sync(server);
        } catch (InterruptedException e) {
            throw e;
        } catch (Exception e) {
            String msg = describe(e);
            set(SyncState.ERROR, "Ошибка: " + msg, msg);
            report(server, true);
            return 30000;
        }
    }

    private long sync(String server) throws Exception {
        JSONObject manifest = new JSONObject(httpGet(server + "/api/manifest", 15000));
        String version = manifest.getString("version");
        JSONArray arr = manifest.getJSONArray("books");
        TreeMap<String, String[]> wanted = new TreeMap<>(); // path -> {sha256, size}
        for (int i = 0; i < arr.length(); i++) {
            JSONObject b = arr.getJSONObject(i);
            String path = b.getString("path");
            if (path.contains("..") || path.startsWith("/")) continue;
            wanted.put(path, new String[]{b.getString("sha256"), String.valueOf(b.getLong("size"))});
        }

        File root = Storage.root();
        if (!root.isDirectory() && !root.mkdirs()) throw new IOException("Не удалось создать папку Books");
        TreeMap<String, String> managed = Storage.loadManaged(ctx);
        List<String> changed = new ArrayList<>();

        // 1. Удаляем то, что убрали с компьютера (только книги, скачанные этим приложением)
        for (String path : new ArrayList<>(managed.keySet())) {
            if (wanted.containsKey(path)) continue;
            File f = new File(root, path);
            if (!f.exists() || f.delete()) {
                managed.remove(path);
                changed.add(f.getPath());
                Storage.removeEmptyParents(f.getParentFile());
            }
        }
        Storage.saveManaged(ctx, managed);

        // 2. Что нужно скачать
        List<String> todo = new ArrayList<>();
        long needBytes = 0;
        for (Map.Entry<String, String[]> e : wanted.entrySet()) {
            String path = e.getKey(), sha = e.getValue()[0];
            long size = Long.parseLong(e.getValue()[1]);
            File f = new File(root, path);
            if (f.isFile() && f.length() == size) {
                if (sha.equals(managed.get(path))) continue;
                if (sha.equals(Storage.sha256(f))) { // уже лежит такой же файл — берём под присмотр
                    managed.put(path, sha);
                    Storage.saveManaged(ctx, managed);
                    continue;
                }
            }
            todo.add(path);
            needBytes += size;
        }
        final int total = wanted.size();
        final int initiallyDone = total - todo.size();
        SyncState.update(s -> {
            s.version = version;
            s.books = new ArrayList<>(wanted.keySet());
            s.total = total;
            s.done = initiallyDone;
        });

        if (!todo.isEmpty()) {
            long free = Storage.freeBytes();
            if (free >= 0 && needBytes > free) {
                throw new IOException("Не хватает места: нужно " + human(needBytes) + ", свободно " + human(free));
            }
            File tmpDir = Storage.tmpDir();
            tmpDir.mkdirs();
            new File(tmpDir, ".nomedia").createNewFile();
            service.holdLocks(true);
            try {
                int done = initiallyDone;
                for (String path : todo) {
                    final int doneNow = done;
                    SyncState.update(s -> {
                        s.state = SyncState.SYNCING;
                        s.message = "Скачиваю " + (doneNow + 1) + " из " + total;
                        s.current = path;
                        s.done = doneNow;
                        s.error = "";
                    });
                    service.refreshNotification();
                    report(server, false);
                    String[] info = wanted.get(path);
                    File target = new File(root, path);
                    downloadWithRetry(server, path, info[0], Long.parseLong(info[1]), target);
                    managed.put(path, info[0]);
                    Storage.saveManaged(ctx, managed);
                    changed.add(target.getPath());
                    done++;
                }
            } finally {
                service.holdLocks(false);
            }
        }
        if (!changed.isEmpty()) {
            MediaScannerConnection.scanFile(ctx, changed.toArray(new String[0]), null, null);
        }
        cleanTmp(wanted);

        final int have = wanted.size();
        SyncState.update(s -> {
            s.done = have;
            s.current = "";
        });
        set(SyncState.OK, have == 0 ? "Учебников пока нет" : "Все учебники скачаны (" + have + ")", "");
        report(server, true);
        return interval * 1000L;
    }

    private void downloadWithRetry(String server, String path, String sha, long size, File target) throws Exception {
        File tmp = new File(Storage.tmpDir(), sha + ".part");
        int failures = 0;
        while (true) {
            try {
                download(server, path, size, tmp);
                if (!sha.equals(Storage.sha256(tmp))) {
                    tmp.delete();
                    throw new IOException("файл повреждён при скачивании");
                }
                File parent = target.getParentFile();
                if (parent != null) parent.mkdirs();
                if (target.exists()) target.delete();
                if (!tmp.renameTo(target)) throw new IOException("не удалось сохранить " + target.getName());
                return;
            } catch (BusyException e) {
                SyncState.update(s -> s.message = "Компьютер занят другими планшетами, жду очереди…");
                service.refreshNotification();
                service.sleepUnlessWoken(10000);
            } catch (IOException e) {
                // Wi-Fi пропал на секунду — докачаем с того же места
                if (++failures >= 4) throw e;
                service.sleepUnlessWoken(5000L * failures);
            }
        }
    }

    private void download(String server, String path, long size, File tmp) throws IOException {
        long have = tmp.exists() ? tmp.length() : 0;
        if (have > size) {
            tmp.delete();
            have = 0;
        }
        if (have == size && size > 0) return;
        HttpURLConnection c = (HttpURLConnection) new URL(server + "/books/" + encodePath(path)).openConnection();
        c.setConnectTimeout(10000);
        c.setReadTimeout(30000);
        if (have > 0) c.setRequestProperty("Range", "bytes=" + have + "-");
        try {
            int code = c.getResponseCode();
            if (code == 503) throw new BusyException();
            if (code == 416) {
                tmp.delete();
                throw new IOException("повтор скачивания");
            }
            if (code == 404) throw new IOException("учебник удалён на компьютере");
            if (code != 200 && code != 206) throw new IOException("сервер ответил " + code);
            boolean append = code == 206;
            if (!append) have = 0;
            final long start = have;
            InputStream in = c.getInputStream();
            OutputStream out = new FileOutputStream(tmp, append);
            try {
                byte[] buf = new byte[128 * 1024];
                long got = start, lastUi = 0;
                int n;
                while ((n = in.read(buf)) > 0) {
                    out.write(buf, 0, n);
                    got += n;
                    long now = System.currentTimeMillis();
                    if (now - lastUi > 500) {
                        lastUi = now;
                        final long g = got;
                        SyncState.update(s -> {
                            s.fileBytes = g;
                            s.fileSize = size;
                        });
                    }
                    if (service.isStopping()) throw new IOException("остановлено");
                }
            } finally {
                out.close();
                in.close();
            }
        } finally {
            c.disconnect();
        }
        if (tmp.length() != size) throw new IOException("связь прервалась");
    }

    /** Удаляет недокачанные файлы книг, которых больше нет в списке. */
    private void cleanTmp(TreeMap<String, String[]> wanted) {
        File[] parts = Storage.tmpDir().listFiles();
        if (parts == null) return;
        Set<String> keep = new LinkedHashSet<>();
        for (String[] v : wanted.values()) keep.add(v[0] + ".part");
        for (File f : parts) {
            if (f.getName().endsWith(".part") && !keep.contains(f.getName())) f.delete();
        }
    }

    // ------------------------------------------------------------ поиск компьютера

    private String findServer() throws InterruptedException {
        String manual = prefs.getString(PREF_MANUAL, "");
        if (!manual.isEmpty()) {
            String url = normalize(manual);
            return ping(url) ? remember(url) : null;
        }
        String last = prefs.getString(PREF_SERVER, "");
        if (!last.isEmpty() && ping(last)) return last;
        SyncState.update(s -> {
            if (!SyncState.OK.equals(s.state)) s.message = "Ищу компьютер с учебниками в сети…";
        });
        for (String url : discover()) {
            if (ping(url)) return remember(url);
        }
        return null;
    }

    private String remember(String url) {
        prefs.edit().putString(PREF_SERVER, url).apply();
        return url;
    }

    static String normalize(String address) {
        String a = address.trim();
        if (a.startsWith("http://")) a = a.substring(7);
        if (a.endsWith("/")) a = a.substring(0, a.length() - 1);
        if (!a.contains(":")) a = a + ":" + DEFAULT_PORT;
        return "http://" + a;
    }

    private boolean ping(String url) {
        try {
            HttpURLConnection c = (HttpURLConnection) new URL(url + "/api/manifest").openConnection();
            c.setConnectTimeout(4000);
            c.setReadTimeout(5000);
            try {
                return c.getResponseCode() == 200;
            } finally {
                c.disconnect();
            }
        } catch (IOException e) {
            return false;
        }
    }

    /** Широковещательный UDP-запрос: компьютер с программой отвечает своим портом. */
    @SuppressLint("MissingPermission")
    private List<String> discover() {
        Set<String> found = new LinkedHashSet<>();
        List<InetAddress> targets = new ArrayList<>();
        try {
            targets.add(InetAddress.getByName("255.255.255.255"));
            WifiManager wifi = (WifiManager) ctx.getSystemService(Context.WIFI_SERVICE);
            DhcpInfo dhcp = wifi == null ? null : wifi.getDhcpInfo();
            if (dhcp != null && dhcp.ipAddress != 0 && dhcp.netmask != 0) {
                int bc = (dhcp.ipAddress & dhcp.netmask) | ~dhcp.netmask;
                byte[] q = new byte[4];
                for (int k = 0; k < 4; k++) q[k] = (byte) ((bc >> (k * 8)) & 0xFF);
                targets.add(InetAddress.getByAddress(q));
            }
        } catch (IOException ignored) {
        }
        byte[] req = "UCHEBNIKI_DISCOVER".getBytes();
        DatagramSocket sock = null;
        try {
            sock = new DatagramSocket();
            sock.setBroadcast(true);
            sock.setSoTimeout(1500);
            for (int attempt = 0; attempt < 2 && found.isEmpty(); attempt++) {
                for (InetAddress t : targets) {
                    try {
                        sock.send(new DatagramPacket(req, req.length, t, DISCOVERY_PORT));
                    } catch (IOException ignored) {
                    }
                }
                byte[] buf = new byte[512];
                while (true) {
                    DatagramPacket p = new DatagramPacket(buf, buf.length);
                    try {
                        sock.receive(p);
                    } catch (SocketTimeoutException e) {
                        break;
                    }
                    try {
                        JSONObject o = new JSONObject(new String(p.getData(), 0, p.getLength(), "UTF-8"));
                        found.add("http://" + p.getAddress().getHostAddress() + ":" + o.optInt("port", DEFAULT_PORT));
                    } catch (Exception ignored) {
                    }
                }
            }
        } catch (IOException ignored) {
        } finally {
            if (sock != null) sock.close();
        }
        return new ArrayList<>(found);
    }

    // ------------------------------------------------------------ отчёт компьютеру

    private void report(String server, boolean force) {
        long now = System.currentTimeMillis();
        if (!force && now - lastReport < 3000) return;
        lastReport = now;
        SyncState st = SyncState.snapshot();
        try {
            JSONObject o = new JSONObject();
            o.put("id", deviceId(ctx));
            o.put("model", Build.MANUFACTURER + " " + Build.MODEL);
            o.put("android", Build.VERSION.RELEASE);
            o.put("app", BuildInfo.versionName(ctx));
            o.put("state", st.state);
            o.put("error", st.error);
            o.put("current", st.current);
            o.put("done", st.done);
            o.put("total", st.total);
            o.put("have", SyncState.OK.equals(st.state) ? st.total : st.done);
            o.put("version", st.version);
            o.put("battery", battery());
            o.put("free", Storage.freeBytes());
            JSONObject r = new JSONObject(httpPost(server + "/api/report", o.toString()));
            String name = r.optString("name", "");
            interval = Math.max(10, r.optInt("interval", 30));
            if (!name.isEmpty()) {
                prefs.edit().putString(PREF_NAME, name).apply();
                SyncState.update(s -> s.name = name);
            }
        } catch (Exception ignored) {
            // отчёт не главное — книги важнее
        }
    }

    private int battery() {
        Intent i = ctx.registerReceiver(null, new IntentFilter(Intent.ACTION_BATTERY_CHANGED));
        if (i == null) return -1;
        int level = i.getIntExtra(BatteryManager.EXTRA_LEVEL, -1), scale = i.getIntExtra(BatteryManager.EXTRA_SCALE, 100);
        return level < 0 || scale <= 0 ? -1 : level * 100 / scale;
    }

    @SuppressLint("HardwareIds")
    static String deviceId(Context ctx) {
        SharedPreferences p = SyncService.prefs(ctx);
        String id = p.getString("device_id", "");
        if (!id.isEmpty()) return id;
        // ANDROID_ID не меняется при переустановке приложения — планшет сохранит своё имя
        String aid = Settings.Secure.getString(ctx.getContentResolver(), Settings.Secure.ANDROID_ID);
        id = aid != null && aid.length() >= 8 && !"9774d56d682e549c".equals(aid) ? "a-" + aid : "u-" + UUID.randomUUID();
        p.edit().putString("device_id", id).apply();
        return id;
    }

    // ------------------------------------------------------------ мелочи

    private void set(String state, String message, String error) {
        SyncState.update(s -> {
            s.state = state;
            s.message = message;
            s.error = error;
            if (!SyncState.SYNCING.equals(state)) s.current = "";
        });
        service.refreshNotification();
    }

    static String encodePath(String path) throws IOException {
        StringBuilder sb = new StringBuilder();
        for (String part : path.split("/")) {
            if (sb.length() > 0) sb.append('/');
            sb.append(URLEncoder.encode(part, "UTF-8").replace("+", "%20"));
        }
        return sb.toString();
    }

    private static String httpGet(String url, int timeout) throws IOException {
        HttpURLConnection c = (HttpURLConnection) new URL(url).openConnection();
        c.setConnectTimeout(8000);
        c.setReadTimeout(timeout);
        try {
            if (c.getResponseCode() != 200) throw new IOException("сервер ответил " + c.getResponseCode());
            return readAll(c.getInputStream());
        } finally {
            c.disconnect();
        }
    }

    private static String httpPost(String url, String json) throws IOException {
        HttpURLConnection c = (HttpURLConnection) new URL(url).openConnection();
        c.setConnectTimeout(8000);
        c.setReadTimeout(10000);
        c.setDoOutput(true);
        c.setRequestMethod("POST");
        c.setRequestProperty("Content-Type", "application/json; charset=utf-8");
        byte[] body = json.getBytes("UTF-8");
        c.setFixedLengthStreamingMode(body.length);
        try {
            OutputStream out = c.getOutputStream();
            out.write(body);
            out.close();
            if (c.getResponseCode() != 200) throw new IOException("сервер ответил " + c.getResponseCode());
            return readAll(c.getInputStream());
        } finally {
            c.disconnect();
        }
    }

    private static String readAll(InputStream in) throws IOException {
        try {
            ByteArrayOutputStream out = new ByteArrayOutputStream();
            byte[] buf = new byte[16384];
            int n;
            while ((n = in.read(buf)) > 0) out.write(buf, 0, n);
            return out.toString("UTF-8");
        } finally {
            in.close();
        }
    }

    static String human(long n) {
        if (n < 1024L * 1024) return Math.max(1, n / 1024) + " КБ";
        if (n < 1024L * 1024 * 1024) return String.format("%.1f МБ", n / 1048576.0);
        return String.format("%.1f ГБ", n / 1073741824.0);
    }

    private static String describe(Exception e) {
        String m = e.getMessage();
        if (e instanceof java.net.ConnectException || e instanceof SocketTimeoutException) {
            return "нет связи с компьютером";
        }
        if (m != null && m.contains("ENOSPC")) return "не хватает места на планшете";
        if (m != null && (m.contains("EACCES") || m.contains("EPERM"))) return "нет доступа к папке Books";
        return m == null || m.isEmpty() ? e.getClass().getSimpleName() : m;
    }
}
