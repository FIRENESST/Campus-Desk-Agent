package cn.campusdesk.calendar;

import android.Manifest;
import android.app.Activity;
import android.app.AlertDialog;
import android.content.ActivityNotFoundException;
import android.content.ClipboardManager;
import android.content.ContentUris;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.res.ColorStateList;
import android.graphics.Color;
import android.graphics.Typeface;
import android.graphics.drawable.GradientDrawable;
import android.graphics.drawable.RippleDrawable;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.provider.CalendarContract;
import android.text.InputType;
import android.text.format.DateFormat;
import android.view.Gravity;
import android.view.View;
import android.view.WindowInsets;
import android.view.WindowManager;
import android.view.inputmethod.InputMethodManager;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;

import java.util.Date;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

public final class MainActivity extends Activity {
    private static final int INK = 0xff17352b;
    private static final int GREEN = 0xff285c46;
    private static final int MUTED = 0xff51675d;
    private static final int ERROR = 0xff9a382d;
    private static final int PAGE = 0xffeff4ef;
    private static final int PERMISSIONS = 1;
    private static final ExecutorService WORKER = Executors.newSingleThreadExecutor();
    private static final String[] CALENDAR_PERMISSIONS = {
        Manifest.permission.READ_CALENDAR, Manifest.permission.WRITE_CALENDAR,
    };

    private EditText link;
    private TextView title;
    private TextView info;
    private TextView status;
    private LinearLayout setup;
    private Button primary;
    private Button paste;
    private Button systemCalendar;
    private Button disconnect;
    private Runnable afterPermission;
    private boolean busy;
    private String notice;
    private String failure;
    private final SharedPreferences.OnSharedPreferenceChangeListener changed = (data, key) -> render();

    @Override protected void onCreate(Bundle state) {
        super.onCreate(state);
        getWindow().setSoftInputMode(WindowManager.LayoutParams.SOFT_INPUT_ADJUST_RESIZE);
        ScrollView scroll = new ScrollView(this);
        scroll.setFillViewport(true);
        scroll.setBackgroundColor(PAGE);
        if (Build.VERSION.SDK_INT >= 35) {
            scroll.setOnApplyWindowInsetsListener((view, insets) -> {
                android.graphics.Insets safe = insets.getInsets(WindowInsets.Type.systemBars() | WindowInsets.Type.displayCutout());
                view.setPadding(safe.left, safe.top, safe.right, Math.max(safe.bottom, insets.getInsets(WindowInsets.Type.ime()).bottom));
                return insets;
            });
        }
        LinearLayout page = column();
        page.setFocusableInTouchMode(true);
        page.setPadding(dp(20), dp(20), dp(20), dp(24));
        scroll.addView(page);

        LinearLayout hero = column();
        hero.setBackground(shape(INK, 24, INK));
        hero.setPadding(dp(22), dp(24), dp(22), dp(24));
        hero.addView(text("中传校园日程", 29, Color.WHITE, true));
        add(hero, text("课程、已报名活动、教室预约和自定义事件，查看一份同步到本机的日历。", 15, 0xffd6e7db, false), 10);
        page.addView(hero);

        LinearLayout card = column();
        card.setBackground(shape(Color.WHITE, 20, 0xffd6e2d8));
        card.setPadding(dp(18), dp(18), dp(18), dp(18));
        add(page, card, 16);
        title = text("", 20, INK, true);
        info = text("", 14, MUTED, false);
        status = text("", 14, MUTED, false);
        status.setAccessibilityLiveRegion(View.ACCESSIBILITY_LIVE_REGION_POLITE);
        status.setPadding(dp(12), dp(9), dp(12), dp(9));
        card.addView(title);
        add(card, info, 8);

        setup = column();
        link = new EditText(this);
        link.setSingleLine(true);
        link.setTextSize(15);
        link.setTextColor(INK);
        link.setHintTextColor(MUTED);
        link.setHint("粘贴个人订阅链接");
        link.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_URI | InputType.TYPE_TEXT_FLAG_NO_SUGGESTIONS);
        link.setImportantForAutofill(View.IMPORTANT_FOR_AUTOFILL_NO);
        link.setSaveEnabled(false);
        link.setBackground(shape(PAGE, 12, 0xffd6e2d8));
        link.setPadding(dp(14), dp(12), dp(14), dp(12));
        link.setMinHeight(dp(48));
        setup.addView(link);
        paste = button("从剪贴板粘贴", false);
        paste.setOnClickListener(view -> {
            ClipboardManager clipboard = getSystemService(ClipboardManager.class);
            android.content.ClipData clip = clipboard.getPrimaryClip();
            CharSequence value = clip != null && clip.getItemCount() > 0 ? clip.getItemAt(0).getText() : null;
            if (value != null) link.setText(value);
            else { failure = "剪贴板里没有订阅链接。"; render(); }
        });
        add(setup, paste, 8);
        add(card, setup, 14);

        primary = button("", true);
        primary.setOnClickListener(view -> {
            if (CalendarSync.connected(this)) {
                withPermission(() -> execute("正在同步…", () -> CalendarSync.refresh(getApplicationContext(), null, () -> false), "已同步到系统日历。"));
            } else {
                String value = link.getText().toString().trim();
                if (value.isEmpty()) { failure = "请先粘贴个人订阅链接。"; render(); return; }
                withPermission(() -> execute("正在连接…", () -> CalendarSync.refresh(getApplicationContext(), value, () -> false), "已同步到系统日历。"));
            }
        });
        add(card, primary, 10);
        systemCalendar = button("打开系统日历", false);
        systemCalendar.setOnClickListener(view -> openSystemCalendar());
        add(card, systemCalendar, 8);
        disconnect = button("取消订阅", false);
        disconnect.setOnClickListener(view -> new AlertDialog.Builder(this).setTitle("取消校园日程订阅？")
            .setMessage("将移除本机的「中传校园日程」。校园助手中的日程和其他手机日历继续保留。")
            .setNegativeButton("返回", null).setPositiveButton("取消订阅", (dialog, which) ->
                withPermission(() -> execute("正在取消订阅…", () -> CalendarSync.disconnect(getApplicationContext()), "已取消本机订阅。")))
            .show());
        disconnect.setTextColor(ERROR);
        add(card, disconnect, 8);
        add(card, status, 12);
        Button privacy = button("隐私说明", false);
        privacy.setOnClickListener(view -> showPrivacy());
        add(page, privacy, 14);
        setContentView(scroll);
        render();
    }

    @Override protected void onStart() {
        super.onStart();
        CalendarSync.data(this).registerOnSharedPreferenceChangeListener(changed);
        render();
    }

    @Override protected void onStop() {
        CalendarSync.data(this).unregisterOnSharedPreferenceChangeListener(changed);
        super.onStop();
    }

    private void withPermission(Runnable action) {
        if (CalendarSync.hasPermission(this)) action.run();
        else {
            afterPermission = action;
            requestPermissions(CALENDAR_PERMISSIONS, PERMISSIONS);
        }
    }

    private void showPrivacy() {
        new AlertDialog.Builder(this).setTitle("隐私说明")
            .setMessage("应用通过你粘贴的个人订阅链接，从校园助手读取课程、报名、预约和自定义事件。链接在本机加密保存。拿到完整链接的人可以读取你的日程，请妥善保管。\n\n"
                + "日历权限用于在系统日历创建、更新和移除「中传校园日程」。点击「从剪贴板粘贴」时，应用才会主动读取剪贴板。\n\n"
                + "在应用取消订阅会移除本机保存的校园日程。还可在校园助手的「日历同步」撤销链接，停止通过该链接获取更新。")
            .setPositiveButton("关闭", null).show();
    }

    private void openSystemCalendar() {
        Uri.Builder day = CalendarContract.CONTENT_URI.buildUpon().appendPath("time");
        ContentUris.appendId(day, System.currentTimeMillis());
        try {
            startActivity(new Intent(Intent.ACTION_VIEW).setData(day.build()));
        } catch (ActivityNotFoundException missing) {
            notice = "请从手机桌面打开日历，再勾选「中传校园日程」。";
            render();
        }
    }

    @Override public void onRequestPermissionsResult(int requestCode, String[] permissions, int[] results) {
        super.onRequestPermissionsResult(requestCode, permissions, results);
        if (requestCode != PERMISSIONS) return;
        Runnable action = afterPermission;
        afterPermission = null;
        if (CalendarSync.hasPermission(this)) {
            if (action != null) action.run();
        } else {
            failure = "需要日历权限。可在手机系统设置里为「中传校园日程」开启权限。";
            render();
        }
    }

    private interface Task { void run() throws CalendarSync.Failure; }

    private void execute(String progress, Task action, String success) {
        if (busy) return;
        getSystemService(InputMethodManager.class).hideSoftInputFromWindow(link.getWindowToken(), 0);
        busy = true;
        failure = null;
        notice = progress;
        render();
        WORKER.execute(() -> {
            String problem = null;
            try { action.run(); }
            catch (CalendarSync.Failure error) { problem = error.getMessage(); }
            String result = problem;
            runOnUiThread(() -> {
                if (isDestroyed()) return;
                busy = false;
                failure = result;
                notice = result == null ? success : null;
                if (result == null) link.setText("");
                render();
            });
        });
    }

    private void render() {
        boolean connected = CalendarSync.connected(this);
        SharedPreferences data = CalendarSync.data(this);
        title.setText(connected ? "个人日程已连接" : "连接个人日程");
        long lastSync = data.getLong(CalendarSync.LAST_SYNC, 0);
        if (connected && lastSync != 0) {
            Date when = new Date(lastSync);
            info.setText("已同步 " + data.getInt(CalendarSync.COUNT, 0) + " 项日程\n上次同步 " + DateFormat.getDateFormat(this).format(when)
                + " " + DateFormat.getTimeFormat(this).format(when) + "\n可在系统日历中查看「中传校园日程」。");
        } else {
            info.setText("在校园助手的「日历同步」创建个人订阅链接，复制到这里。");
        }
        setup.setVisibility(connected ? View.GONE : View.VISIBLE);
        systemCalendar.setVisibility(connected ? View.VISIBLE : View.GONE);
        disconnect.setVisibility(connected ? View.VISIBLE : View.GONE);
        systemCalendar.setEnabled(!busy);
        disconnect.setEnabled(!busy);
        paste.setEnabled(!busy);
        link.setEnabled(!busy);
        primary.setEnabled(!busy);
        primary.setAlpha(busy ? 0.55f : 1f);
        primary.setText(connected ? "立即同步" : "连接并同步");
        String error = busy ? null : failure != null ? failure : data.getString(CalendarSync.ERROR, null);
        String message = error != null ? error : notice;
        status.setTextColor(error != null ? ERROR : MUTED);
        status.setBackground(error != null ? shape(0xfffcf0e9, 9, 0xffedd2c8) : shape(PAGE, 9, 0xffd6e2d8));
        status.setText(message);
        status.setVisibility(message == null ? View.GONE : View.VISIBLE);
    }

    private int dp(int value) { return Math.round(value * getResources().getDisplayMetrics().density); }

    private LinearLayout column() {
        LinearLayout view = new LinearLayout(this);
        view.setOrientation(LinearLayout.VERTICAL);
        return view;
    }

    private void add(LinearLayout parent, View child, int top) {
        LinearLayout.LayoutParams params = new LinearLayout.LayoutParams(-1, -2);
        params.topMargin = dp(top);
        parent.addView(child, params);
    }

    private TextView text(String value, int size, int color, boolean bold) {
        TextView view = new TextView(this);
        view.setText(value);
        view.setTextSize(size);
        view.setTextColor(color);
        if (bold) view.setTypeface(Typeface.DEFAULT, Typeface.BOLD);
        view.setLineSpacing(dp(2), 1f);
        return view;
    }

    private Button button(String label, boolean filled) {
        Button button = new Button(this);
        button.setText(label);
        button.setTextSize(15);
        button.setAllCaps(false);
        button.setMinHeight(dp(48));
        button.setGravity(Gravity.CENTER);
        button.setTextColor(filled ? Color.WHITE : GREEN);
        GradientDrawable background = shape(filled ? GREEN : Color.WHITE, 12, filled ? GREEN : 0xffcbdace);
        button.setBackground(new RippleDrawable(ColorStateList.valueOf(filled ? 0x33ffffff : 0x22285c46), background, null));
        return button;
    }

    private GradientDrawable shape(int color, int radius, int border) {
        GradientDrawable shape = new GradientDrawable();
        shape.setColor(color);
        shape.setCornerRadius(dp(radius));
        shape.setStroke(dp(1), border);
        return shape;
    }
}
