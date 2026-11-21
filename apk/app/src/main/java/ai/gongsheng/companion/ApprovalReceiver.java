package ai.gongsheng.companion;

import android.app.NotificationManager;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.os.Handler;
import android.os.Looper;
import android.widget.Toast;
import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;

/**
 * T6 审批通知按钮（2026-11-21）：点通知上的「批准/拒绝」直接裁决危险指令审批单，
 * 不必再打开 webui 审批面板。
 *
 * 安全与边界：
 *   · 裁决走 webui 同一个代理端点 /api/approvals/decide，四道校验**原样**：
 *     Cookie(xiaoman_token) + X-Xiaoman-Request: 1。令牌只从本机 prefs 读，
 *     不进通知、不进 ntfy、不离开手机。
 *   · decided_by=boss-phone，与 webui 面板（boss-webui）在账本里可区分（留痕）。
 *   · 失败绝不静默成功：网络/服务不可达时 Toast 报错并**保留通知**，可再点重试；
 *     只有服务端确认 ok 才划掉通知。
 */
public class ApprovalReceiver extends BroadcastReceiver {

    @Override
    public void onReceive(Context context, Intent intent) {
        final String approvalId = intent.getStringExtra("approval_id");
        final String decision = intent.getStringExtra("decision");
        final int notifId = intent.getIntExtra("notif_id", -1);
        boolean decisionOk = "approve".equals(decision) || "deny".equals(decision);
        if (approvalId == null || approvalId.isEmpty() || !decisionOk) {
            return;
        }
        final PendingResult pr = goAsync();
        final Context app = context.getApplicationContext();
        new Thread(() -> {
            String result;
            try {
                SharedPreferences prefs =
                        app.getSharedPreferences("heci_config", Context.MODE_PRIVATE);
                String base = prefs.getString("server_url", "");
                String token = prefs.getString("server_token", "");
                if (base.isEmpty() || token.isEmpty()) {
                    result = "未配置服务地址/令牌，请先在 APP 设置页保存";
                } else {
                    JSONObject body = new JSONObject();
                    body.put("approval_id", approvalId);
                    body.put("decision", decision);
                    body.put("decided_by", "boss-phone");
                    result = decide(app, base, token, body.toString(), decision, approvalId, notifId);
                }
            } catch (Exception e) {
                result = "裁决失败：" + e.getClass().getSimpleName();
            }
            final String msg = result;
            new Handler(Looper.getMainLooper()).post(() ->
                    Toast.makeText(app, "审批 · " + msg, Toast.LENGTH_LONG).show());
            pr.finish();
        }, "heci-approve").start();
    }

    /** 同步 POST 裁决。成功返回结果文案并把通知划掉；失败保留通知以便重试。 */
    private static String decide(Context app, String base, String token, String bodyJson,
                                 String decision, String approvalId, int notifId) {
        HttpURLConnection c = null;
        try {
            c = (HttpURLConnection) new URL(base + "/api/approvals/decide").openConnection();
            c.setRequestMethod("POST");
            c.setConnectTimeout(8000);
            c.setReadTimeout(8000);
            c.setDoOutput(true);
            c.setRequestProperty("Content-Type", "application/json; charset=utf-8");
            // webui 第③道校验：POST 必须带自定义标记（触发 CORS 预检的那个）
            c.setRequestProperty("X-Xiaoman-Request", "1");
            // 第④道：令牌 Cookie —— 与 WebView 注入的完全同一枚
            c.setRequestProperty("Cookie", "xiaoman_token=" + token);
            try (OutputStream os = c.getOutputStream()) {
                os.write(bodyJson.getBytes(StandardCharsets.UTF_8));
            }
            int code = c.getResponseCode();
            InputStream is = code >= 400 ? c.getErrorStream() : c.getInputStream();
            String resp = readAll(is);
            boolean ok = code >= 200 && code < 300 && resp.contains("\"ok\": true");
            if (ok) {
                if (notifId > 0) {
                    NotificationManager nm =
                            app.getSystemService(NotificationManager.class);
                    if (nm != null) {
                        nm.cancel(notifId);
                    }
                }
                return ("approve".equals(decision) ? "已批准 " : "已拒绝 ") + approvalId;
            }
            return "裁决失败(HTTP " + code + ")"
                    + (resp.isEmpty() ? "" : "：" + abbrev(resp));
        } catch (Exception e) {
            return "裁决失败：" + e.getClass().getSimpleName()
                    + (e.getMessage() == null ? "" : " " + abbrev(e.getMessage()));
        } finally {
            if (c != null) {
                c.disconnect();
            }
        }
    }

    private static String readAll(InputStream is) {
        if (is == null) {
            return "";
        }
        StringBuilder sb = new StringBuilder();
        try (BufferedReader in = new BufferedReader(
                new InputStreamReader(is, StandardCharsets.UTF_8))) {
            String line;
            while ((line = in.readLine()) != null) {
                sb.append(line);
            }
        } catch (Exception ignore) {
            // 读不出 body 不影响按状态码判定
        }
        return sb.toString();
    }

    private static String abbrev(String s) {
        return s.length() > 80 ? s.substring(0, 80) + "…" : s;
    }
}
