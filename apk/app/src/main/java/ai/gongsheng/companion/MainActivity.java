package ai.gongsheng.companion;

import android.annotation.SuppressLint;
import android.app.Activity;
import android.app.AlertDialog;
import android.content.Intent;
import android.content.SharedPreferences;
import android.graphics.Color;
import android.os.Build;
import android.os.Bundle;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.webkit.CookieManager;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.TextView;

/**
 * 鹤辞安卓壳（APK-1，2026-11-21 定稿方向）。
 *
 * 老板拍板：**手机端 UI 必须与桌面端完全一致** —— 所以壳只做两件事：
 *   1. 把 webui 页面（桌面端同一个界面）装进 WebView，像素级一致；
 *   2. 解决「手机怎么连上后端」—— 令牌直接注入 Cookie（xiaoman_token=…），
 *      不依赖 /?token= 引导（引导对全新客户端返回 401 的服务端问题待查）。
 *
 * 连不上时弹窗（重试 / 重新配置），APP 本身永远能打开。
 */
public class MainActivity extends Activity {

    private static final String PREFS = "heci_config";
    private static final String KEY_URL = "server_url";
    private static final String KEY_TOKEN = "server_token";
    private static final String KEY_PUSH = "push_topic";

    private static final int ACCENT = 0xFF0866FF;
    private static final int PAPER = 0xFFF6F7FB;
    private static final int INK = 0xFF1C1E21;
    private static final int DIM = 0xFF5F6266;

    private SharedPreferences prefs;
    private EditText urlInput;
    private EditText tokenInput;
    private EditText pushInput;
    private WebView web;
    private String currentUrl;
    private boolean dialogShowing;

    // ------------------------------------------------------------------ 生命周期

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        prefs = getSharedPreferences(PREFS, MODE_PRIVATE);
        if (prefs.getString(KEY_URL, "").isEmpty()) {
            showConfig();
        } else {
            openWeb();
            ensurePush();   // APP 重启时自动恢复推送订阅
        }
    }

    // ------------------------------------------------------------------ 配置页

    private void showConfig() {
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setBackgroundColor(PAPER);
        int pad = dp(20);
        root.setPadding(pad, dp(48), pad, pad);

        TextView title = new TextView(this);
        title.setText(R.string.config_title);
        title.setTextSize(20);
        title.setTextColor(INK);
        title.setTypeface(null, android.graphics.Typeface.BOLD);
        title.setGravity(Gravity.CENTER);
        root.addView(title);

        // 版本标记：用来一眼分辨装的是不是这个包（旧包没有这行字）
        TextView ver = new TextView(this);
        ver.setText(R.string.config_version);
        ver.setTextSize(12);
        ver.setTextColor(ACCENT);
        ver.setGravity(Gravity.CENTER);
        ver.setPadding(0, dp(4), 0, 0);
        root.addView(ver);

        TextView hint = new TextView(this);
        hint.setText(R.string.config_hint);
        hint.setTextSize(13);
        hint.setTextColor(DIM);
        hint.setPadding(0, dp(16), 0, dp(8));
        root.addView(hint);

        urlInput = new EditText(this);
        urlInput.setHint(R.string.config_url_hint);
        urlInput.setSingleLine(true);
        urlInput.setTextColor(INK);
        urlInput.setText(prefs.getString(KEY_URL, ""));
        root.addView(urlInput);

        tokenInput = new EditText(this);
        tokenInput.setHint(R.string.config_token_hint);
        tokenInput.setSingleLine(true);
        tokenInput.setTextColor(INK);
        tokenInput.setText(prefs.getString(KEY_TOKEN, ""));
        root.addView(tokenInput);

        pushInput = new EditText(this);
        pushInput.setHint(R.string.config_push_hint);
        pushInput.setSingleLine(true);
        pushInput.setTextColor(INK);
        pushInput.setText(prefs.getString(KEY_PUSH, ""));
        root.addView(pushInput);

        Button save = new Button(this);
        save.setText(R.string.config_save);
        save.setTextColor(Color.WHITE);
        save.setBackgroundResource(R.drawable.send_btn);
        save.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                onSave();
            }
        });
        LinearLayout.LayoutParams saveLp = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        saveLp.topMargin = dp(12);
        root.addView(save, saveLp);

        setContentView(root);
        // 登录/设置页保留系统白条（老板要求：登录界面有就行）
        if (getActionBar() != null) {
            getActionBar().show();
        }
    }

    private void onSave() {
        String url = urlInput.getText().toString().trim();
        String token = tokenInput.getText().toString().trim();
        String push = pushInput.getText().toString().trim();
        if (!url.contains("://")) {
            url = "http://" + url;
        }
        while (url.endsWith("/")) {
            url = url.substring(0, url.length() - 1);
        }
        prefs.edit()
                .putString(KEY_URL, url)
                .putString(KEY_TOKEN, token)
                .putString(KEY_PUSH, push)
                .apply();
        ensurePush();
        openWeb();
    }

    /** 开启/关闭内置推送：topic 非空 → 前台服务常驻订阅 ntfy 流；空 → 停止。 */
    private void ensurePush() {
        String topic = prefs.getString(KEY_PUSH, "");
        Intent i = new Intent(this, NtfyService.class);
        if (topic.isEmpty()) {
            stopService(i);
            return;
        }
        // Android 13+ 通知权限要运行时申请；拒绝的话通知不弹，稍后可在系统设置里补
        if (Build.VERSION.SDK_INT >= 33 && checkSelfPermission(
                android.Manifest.permission.POST_NOTIFICATIONS)
                != android.content.pm.PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[]{android.Manifest.permission.POST_NOTIFICATIONS}, 7);
        }
        if (Build.VERSION.SDK_INT >= 26) {
            startForegroundService(i);
        } else {
            startService(i);
        }
    }

    // ------------------------------------------------------------------ WebView

    @SuppressLint("SetJavaScriptEnabled")
    private void openWeb() {
        currentUrl = prefs.getString(KEY_URL, "");
        String token = prefs.getString(KEY_TOKEN, "");

        CookieManager cm = CookieManager.getInstance();
        cm.setAcceptCookie(true);
        // 令牌直接注入 Cookie：webui 第④道校验就是比对 Cookie 里的令牌，
        // 客户端持有令牌即等价于已登录 —— 不走 /?token= 引导。
        if (!token.isEmpty()) {
            cm.setCookie(currentUrl, "xiaoman_token=" + token + "; Path=/");
            cm.flush();
        }

        if (web == null) {
            web = new WebView(this);
            web.getSettings().setJavaScriptEnabled(true);
            web.getSettings().setDomStorageEnabled(true);
            web.setBackgroundColor(PAPER);
            web.setWebViewClient(new WebViewClient() {
                @Override
                public void onReceivedError(WebView view, WebResourceRequest req,
                                            WebResourceError err) {
                    // 只有主框架加载失败才算「连不上」；页面内部资源失败交给页面自己
                    if (req.isForMainFrame()) {
                        showFailDialog(String.valueOf(err.getDescription()));
                    }
                }
            });
        }
        setContentView(web);
        // 聊天界面隐藏系统白条（ActionBar）——页面自带头部；设置页保留
        if (getActionBar() != null) {
            getActionBar().hide();
        }
        web.loadUrl(currentUrl + "/");
    }

    private void reloadWeb() {
        if (web != null && currentUrl != null) {
            dialogShowing = false;
            openWeb();   // 重新注入 Cookie 并加载
        } else {
            showConfig();
        }
    }

    /** 连接失败：弹窗展示（按老板要求红色横幅不用），给出「重试 / 重新配置」。 */
    private void showFailDialog(String detail) {
        if (dialogShowing) {
            return;
        }
        dialogShowing = true;
        new AlertDialog.Builder(this)
                .setTitle(R.string.dialog_title)
                .setMessage((detail == null ? "" : detail) + getString(R.string.dialog_msg_suffix))
                .setPositiveButton(R.string.dialog_retry,
                        new android.content.DialogInterface.OnClickListener() {
                            @Override
                            public void onClick(android.content.DialogInterface d, int w) {
                                dialogShowing = false;
                                reloadWeb();
                            }
                        })
                .setNegativeButton(R.string.dialog_reconfig,
                        new android.content.DialogInterface.OnClickListener() {
                            @Override
                            public void onClick(android.content.DialogInterface d, int w) {
                                dialogShowing = false;
                                showConfig();
                            }
                        })
                .setOnCancelListener(new android.content.DialogInterface.OnCancelListener() {
                    @Override
                    public void onCancel(android.content.DialogInterface d) {
                        dialogShowing = false;
                        reloadWeb();
                    }
                })
                .show();
    }

    // ------------------------------------------------------------------ 导航

    @Override
    public boolean onCreateOptionsMenu(android.view.Menu menu) {
        menu.add(R.string.menu_reconfig);
        return true;
    }

    @Override
    public boolean onOptionsItemSelected(android.view.MenuItem item) {
        showConfig();
        return true;
    }

    @Override
    public boolean onKeyDown(int keyCode, android.view.KeyEvent event) {
        if (keyCode == android.view.KeyEvent.KEYCODE_BACK && web != null && web.canGoBack()
                && web.getVisibility() == View.VISIBLE) {
            web.goBack();
            return true;
        }
        return super.onKeyDown(keyCode, event);
    }

    private int dp(int v) {
        return Math.round(v * getResources().getDisplayMetrics().density);
    }
}
