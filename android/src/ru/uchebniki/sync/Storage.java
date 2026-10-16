package ru.uchebniki.sync;

import android.Manifest;
import android.content.Context;
import android.content.pm.PackageManager;
import android.os.Build;
import android.os.Environment;
import android.os.StatFs;

import org.json.JSONObject;

import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.util.Iterator;
import java.util.TreeMap;

/** Папка Books и список книг, которые скачало это приложение (только их оно может удалять). */
final class Storage {
    static final String FOLDER = "Books";

    static File root() {
        return new File(Environment.getExternalStorageDirectory(), FOLDER);
    }

    static File tmpDir() {
        return new File(root(), ".uchebniki-tmp");
    }

    static boolean canWrite(Context ctx) {
        if (Build.VERSION.SDK_INT >= 30) {
            return Environment.isExternalStorageManager();
        }
        if (Build.VERSION.SDK_INT >= 23) {
            return ctx.checkSelfPermission(Manifest.permission.WRITE_EXTERNAL_STORAGE)
                    == PackageManager.PERMISSION_GRANTED;
        }
        return true;
    }

    static long freeBytes() {
        try {
            File dir = root().exists() ? root() : Environment.getExternalStorageDirectory();
            return new StatFs(dir.getPath()).getAvailableBytes();
        } catch (IllegalArgumentException e) {
            return -1;
        }
    }

    /** path -> sha256 файлов, скачанных приложением. */
    static TreeMap<String, String> loadManaged(Context ctx) {
        TreeMap<String, String> map = new TreeMap<>();
        File f = new File(ctx.getFilesDir(), "managed.json");
        if (!f.exists()) return map;
        try {
            JSONObject o = new JSONObject(readText(f));
            Iterator<String> keys = o.keys();
            while (keys.hasNext()) {
                String k = keys.next();
                map.put(k, o.getString(k));
            }
        } catch (Exception ignored) {
            // повреждённый файл — начнём заново, книги просто перепроверятся
        }
        return map;
    }

    static void saveManaged(Context ctx, TreeMap<String, String> map) throws IOException {
        File f = new File(ctx.getFilesDir(), "managed.json");
        File tmp = new File(ctx.getFilesDir(), "managed.json.tmp");
        FileOutputStream out = new FileOutputStream(tmp);
        try {
            out.write(new JSONObject(map).toString().getBytes("UTF-8"));
            out.getFD().sync();
        } finally {
            out.close();
        }
        if (!tmp.renameTo(f)) throw new IOException("Не удалось сохранить список книг");
    }

    static String sha256(File f) throws IOException {
        MessageDigest md;
        try {
            md = MessageDigest.getInstance("SHA-256");
        } catch (NoSuchAlgorithmException e) {
            throw new IOException(e);
        }
        InputStream in = new FileInputStream(f);
        try {
            byte[] buf = new byte[256 * 1024];
            int n;
            while ((n = in.read(buf)) > 0) md.update(buf, 0, n);
        } finally {
            in.close();
        }
        StringBuilder sb = new StringBuilder();
        for (byte b : md.digest()) sb.append(String.format("%02x", b));
        return sb.toString();
    }

    /** Удаляет пустые папки от dir вверх до Books. */
    static void removeEmptyParents(File dir) {
        File root = root();
        while (dir != null && !dir.equals(root) && dir.getPath().startsWith(root.getPath())) {
            String[] left = dir.list();
            if (left == null || left.length > 0 || !dir.delete()) return;
            dir = dir.getParentFile();
        }
    }

    private static String readText(File f) throws IOException {
        InputStream in = new FileInputStream(f);
        try {
            byte[] data = new byte[(int) f.length()];
            int off = 0, n;
            while (off < data.length && (n = in.read(data, off, data.length - off)) > 0) off += n;
            return new String(data, 0, off, "UTF-8");
        } finally {
            in.close();
        }
    }
}
