package cn.campusdesk.calendar;

import android.Manifest;
import android.content.Context;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.os.SystemClock;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.net.URI;
import java.nio.ByteBuffer;
import java.nio.charset.StandardCharsets;
import java.security.GeneralSecurityException;
import java.util.Locale;
import java.util.Map;
import java.util.function.BooleanSupplier;

import javax.net.ssl.HttpsURLConnection;

final class CalendarSync {
    private static final String LINK = "link";
    private static final String ETAG = "etag";
    static final String LAST_SYNC = "lastSync";
    static final String COUNT = "count";
    static final String ERROR = "error";
    private static final int MAX_FEED_BYTES = 4 * 1024 * 1024;

    static final class Failure extends IOException {
        Failure(String message) {
            super(message);
        }
    }

    static SharedPreferences data(Context context) {
        return context.getSharedPreferences("subscription", Context.MODE_PRIVATE);
    }

    static boolean connected(Context context) {
        return data(context).contains(LINK);
    }

    static boolean hasPermission(Context context) {
        return context.checkSelfPermission(Manifest.permission.READ_CALENDAR) == PackageManager.PERMISSION_GRANTED
            && context.checkSelfPermission(Manifest.permission.WRITE_CALENDAR) == PackageManager.PERMISSION_GRANTED;
    }

    static synchronized void refresh(Context context, String pastedLink, BooleanSupplier canceled) throws Failure {
        SharedPreferences data = data(context);
        try {
            checkCanceled(canceled);
            if (!hasPermission(context)) throw new Failure("请在系统设置中允许访问日历，再同步校园日程。");
            String saved = data.getString(LINK, null);
            if (pastedLink != null && saved != null) throw new Failure("更换订阅前，请先取消当前订阅。");
            if (pastedLink == null && saved == null) throw new Failure("请先连接个人订阅链接。");
            URI uri = subscription(saved, pastedLink);
            if (saved == null) saved = SubscriptionKey.protect(uri.toString());
            CalendarStore calendar = new CalendarStore(context.getContentResolver());
            long id = calendar.find();
            String etag = id < 0 || pastedLink != null ? "" : data.getString(ETAG, "");
            int count = data.getInt(COUNT, 0);
            HttpsURLConnection https = (HttpsURLConnection) uri.toURL().openConnection();
            try {
                https.setInstanceFollowRedirects(false);
                https.setConnectTimeout(12_000);
                https.setReadTimeout(15_000);
                https.setUseCaches(false);
                https.setRequestProperty("Accept", "text/calendar");
                if (!etag.isEmpty()) https.setRequestProperty("If-None-Match", etag);
                int status = https.getResponseCode();
                if (status != 304 || etag.isEmpty()) {
                    Map<String, CalendarFeed.Event> events = readFeed(https, status, canceled);
                    if (!etag.isEmpty() && !data.edit().remove(ETAG).commit()) {
                        throw new Failure("暂时无法保存订阅状态，请重试。");
                    }
                    calendar.write(id, events, canceled);
                    etag = safeTag(https.getHeaderField("ETag"));
                    count = events.size();
                }
            } catch (IOException error) {
                if (error instanceof Failure) throw (Failure) error;
                throw new Failure("暂时无法连接校园助手，请检查网络后重试。");
            } finally {
                https.disconnect();
            }
            checkCanceled(canceled);
            boolean recorded = data.edit().putString(LINK, saved).putString(ETAG, etag).putInt(COUNT, count)
                .putLong(LAST_SYNC, System.currentTimeMillis()).remove(ERROR).commit();
            if (!recorded) throw new Failure("暂时无法保存订阅状态，请重试。");
            CalendarJob.schedule(context);
        } catch (Exception error) {
            checkCanceled(canceled);
            Failure failure = describe(error);
            if (connected(context)) data.edit().putString(ERROR, failure.getMessage()).apply();
            throw failure;
        }
    }

    private static URI subscription(String saved, String pasted) throws Failure {
        try {
            return CalendarFeed.link(pasted != null ? pasted : SubscriptionKey.reveal(saved));
        } catch (GeneralSecurityException error) {
            throw new Failure("无法读取本机的订阅链接，请取消订阅后重新连接。");
        } catch (IOException error) {
            if (pasted == null) throw new Failure("无法读取本机的订阅链接，请取消订阅后重新连接。");
            throw new Failure(error.getMessage());
        }
    }

    private static Map<String, CalendarFeed.Event> readFeed(HttpsURLConnection https, int status, BooleanSupplier canceled)
            throws IOException {
        if (status == 401 || status == 403 || status == 404 || status == 410) {
            throw new Failure("订阅链接已失效或被撤销，请到校园助手重新创建链接。");
        }
        if (status != 200) throw new Failure("校园助手暂时无法提供日程，请稍后重试。");
        String type = https.getContentType();
        if (type == null || !"text/calendar".equals(type.split(";", 2)[0].trim().toLowerCase(Locale.ROOT))) {
            throw new Failure("该链接没有返回日程，请核对订阅地址。");
        }
        if (https.getContentLengthLong() > MAX_FEED_BYTES) throw new Failure("订阅返回的日程文件太大。");
        long deadline = SystemClock.elapsedRealtime() + 45_000;
        ByteArrayOutputStream bytes = new ByteArrayOutputStream();
        try (InputStream stream = https.getInputStream()) {
            byte[] chunk = new byte[8192];
            for (int length; (length = stream.read(chunk)) != -1;) {
                checkCanceled(canceled);
                if (SystemClock.elapsedRealtime() > deadline) throw new Failure("同步超时，请检查网络后重试。");
                if (bytes.size() + length > MAX_FEED_BYTES) throw new Failure("订阅返回的日程文件太大。");
                bytes.write(chunk, 0, length);
            }
        }
        try {
            String text = StandardCharsets.UTF_8.newDecoder().decode(ByteBuffer.wrap(bytes.toByteArray())).toString();
            return CalendarFeed.parse(text);
        } catch (IOException error) {
            throw new Failure("订阅返回的日程格式有误，已保留手机里的日程。");
        }
    }

    private static String safeTag(String value) {
        return value != null && value.matches("(?:W/)?\"[0-9a-f]{64}\"") ? value : "";
    }

    static synchronized void disconnect(Context context) throws Failure {
        try {
            if (!hasPermission(context)) throw new Failure("请先允许访问日历，以移除已同步的校园日程。");
            new CalendarStore(context.getContentResolver()).remove();
            if (!data(context).edit().clear().commit()) throw new Failure("暂时无法清除订阅，请重试。");
            CalendarJob.cancel(context);
        } catch (Exception error) {
            throw describe(error);
        }
    }

    static void checkCanceled(BooleanSupplier canceled) throws Failure {
        if (canceled.getAsBoolean()) throw new Failure("本次同步已暂停。");
    }

    private static Failure describe(Exception error) {
        if (error instanceof Failure) return (Failure) error;
        if (error instanceof SecurityException) return new Failure("请确认应用有访问日历的权限，再同步试试。");
        return new Failure("暂时无法更新手机日历，请稍后重试。");
    }
}
