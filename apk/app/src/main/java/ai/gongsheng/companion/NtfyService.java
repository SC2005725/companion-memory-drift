package ai.gongsheng.companion;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Intent;
import android.content.SharedPreferences;
import android.os.Build;
import android.os.IBinder;
import org.json.JSONArray;
import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;

/**
 * 鹤辞内置推送（2026-11-21）：把 ntfy 订阅**内嵌进共生 APP**——
 * 手机上不再需要独立的 ntfy 应用，她的消息直接以系统通知弹出。
 *
 * 原理：ntfy 的订阅就是一个 HTTP 长连接流（GET /<topic>/json，每行一个 JSON 事件，
 * ~45s 一次 keepalive），框架 HttpURLConnection 就能订阅，零第三方依赖。
 *
 * 可靠性：断线自动重连，并带 since=<最后收到的消息 id> 续传——
 * ntfy 服务端缓存 12 小时，手机离线期间的消息重连后补投，不丢。
 *
 * 通知分两路：一条低优先级的常驻连接通知（Android 8+ 前台服务的硬要求），
 * 一条高优先级的消息通知（点按打开鹤辞主界面）。
 */
public class NtfyService extends Service {

    private static final String PREFS = "heci_config";
    private static final String CH_SVC = "heci_svc";     // 常驻连接（低优先级）
    private static final String CH_MSG = "heci_msg";     // 她的消息（高优先级）
    private static final String KEY_SINCE = "ntfy_since";

    private volatile boolean running;
    private Thread worker;

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    @Override
    public void onCreate() {
        super.onCreate();
        if (Build.VERSION.SDK_INT >= 26) {
            NotificationManager nm = getSystemService(NotificationManager.class);
            nm.createNotificationChannel(new NotificationChannel(
                    CH_SVC, "常驻连接", NotificationManager.IMPORTANCE_LOW));
            nm.createNotificationChannel(new NotificationChannel(
                    CH_MSG, "鹤辞的消息", NotificationManager.IMPORTANCE_HIGH));
        }
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        SharedPreferences prefs = getSharedPreferences(PREFS, MODE_PRIVATE);
        String topic = prefs.getString("push_topic", "");
        if (topic.isEmpty()) {
            stopSelf();
            return START_NOT_STICKY;
        }
        startForeground(1, buildSvcNotification());
        if (worker != null && worker.isAlive()) {
            return START_STICKY;
        }
        running = true;
        final String t = topic;
        worker = new Thread(() -> pollLoop(t), "heci-ntfy");
        worker.start();
        return START_STICKY;
    }

    private Notification buildSvcNotification() {
        return buildSvcNotification("启动中…");
    }

    /** 常驻通知 = 实时诊断条：连接状态/重试/权限缺失都写在这里，手机上一眼可见。 */
    private Notification buildSvcNotification(String state) {
        Intent open = new Intent(this, MainActivity.class);
        PendingIntent pi = PendingIntent.getActivity(this, 0, open,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
        boolean permOk = Build.VERSION.SDK_INT < 33
                || checkSelfPermission(android.Manifest.permission.POST_NOTIFICATIONS)
                        == android.content.pm.PackageManager.PERMISSION_GRANTED;
        String text = state + (permOk ? "" : " ｜⚠ 通知权限未授予（系统设置→应用→鹤辞→通知）");
        Notification.Builder b = Build.VERSION.SDK_INT >= 26
                ? new Notification.Builder(this, CH_SVC)
                : new Notification.Builder(this);
        return b.setContentTitle("鹤辞 · 推送")
                .setContentText(text)
                .setStyle(new Notification.BigTextStyle().bigText(text))
                .setSmallIcon(android.R.drawable.stat_notify_sync)
                .setContentIntent(pi)
                .setOngoing(true)
                .build();
    }

    private void updateSvc(String state) {
        NotificationManager nm = getSystemService(NotificationManager.class);
        if (nm != null) {
            nm.notify(1, buildSvcNotification(state));
        }
    }

    private void pollLoop(String topic) {
        SharedPreferences prefs = getSharedPreferences(PREFS, MODE_PRIVATE);
        int retries = 0;
        while (running) {
            HttpURLConnection c = null;
            try {
                String since = prefs.getString(KEY_SINCE, "");
                String url = "https://ntfy.sh/" + topic + "/json?since="
                        + (since.isEmpty() ? "none" : since);
                c = (HttpURLConnection) new URL(url).openConnection();
                c.setConnectTimeout(15000);
                c.setReadTimeout(120000);   // 流是长连的，keepalive ~45s 一次，不会触超时
                updateSvc("连接 ntfy 中（topic " + topic + "）…");
                BufferedReader in = new BufferedReader(
                        new InputStreamReader(c.getInputStream(), StandardCharsets.UTF_8));
                updateSvc("已连接 · 监听她的消息（topic " + topic + "）");
                retries = 0;
                String line;
                while (running && (line = in.readLine()) != null) {
                    line = line.trim();
                    if (line.isEmpty()) {
                        continue;
                    }
                    JSONObject ev;
                    try {
                        ev = new JSONObject(line);
                    } catch (Exception ignore) {
                        continue;
                    }
                    if (!"message".equals(ev.optString("event"))) {
                        continue;   // open / keepalive 不弹通知
                    }
                    String id = ev.optString("id", "");
                    if (!id.isEmpty()) {
                        prefs.edit().putString(KEY_SINCE, id).apply();
                    }
                    notifyMsg(ev.optString("title", "鹤辞"), ev.optString("message", ""),
                            ev.optJSONArray("actions"));
                }
                if (running) {
                    throw new java.io.IOException("服务器关闭了连接");
                }
            } catch (Exception e) {
                retries += 1;
                if (running) {
                    updateSvc("第 " + retries + " 次重连（8 秒后）："
                            + e.getClass().getSimpleName() + " " + str(e.getMessage()));
                }
            }
            if (c != null) {
                c.disconnect();
            }
            if (running) {
                try {
                    Thread.sleep(8000);
                } catch (InterruptedException ie) {
                    return;
                }
            }
        }
    }

    private static String str(Object o) {
        return o == null ? "" : String.valueOf(o);
    }

    private void notifyMsg(String title, String msg, JSONArray actions) {
        Intent open = new Intent(this, MainActivity.class);
        PendingIntent pi = PendingIntent.getActivity(this, 1, open,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
        // T6（2026-11-21）：审批通知用独立通知位（同一审批一张卡，多张审批并存
        // 互不覆盖；巡查器每轮重推同一审批时同 id 覆盖，天然去重）。
        // 普通消息仍走固定 id=2（v0.5 行为不变）。
        String approvalId = approvalIdOf(actions);
        int notifId = (approvalId != null)
                ? 2000 + (approvalId.hashCode() & 0x7fffffff) % 90000 : 2;
        Notification.Builder b = Build.VERSION.SDK_INT >= 26
                ? new Notification.Builder(this, CH_MSG)
                : new Notification.Builder(this);
        b.setContentTitle(title)
                .setContentText(msg)
                .setStyle(new Notification.BigTextStyle().bigText(msg))
                .setSmallIcon(android.R.drawable.ic_dialog_email)
                .setContentIntent(pi)
                .setAutoCancel(true);
        if (approvalId != null) {
            addApprovalButtons(b, approvalId, notifId);
        }
        NotificationManager nm = getSystemService(NotificationManager.class);
        if (nm != null) {
            nm.notify(notifId, b.build());
        }
    }

    /** 从 ntfy actions 里认出本 APP 的审批按钮载荷（gongsheng_cmd=approval_decide）。 */
    private static String approvalIdOf(JSONArray actions) {
        if (actions == null) {
            return null;
        }
        for (int i = 0; i < actions.length(); i++) {
            JSONObject a = actions.optJSONObject(i);
            if (a == null || !"broadcast".equals(a.optString("action"))) {
                continue;
            }
            JSONObject ex = a.optJSONObject("extras");
            if (ex != null && "approval_decide".equals(ex.optString("gongsheng_cmd"))
                    && !ex.optString("approval_id").isEmpty()) {
                return ex.optString("approval_id");
            }
        }
        return null;
    }

    /** T6：给审批通知加「批准/拒绝」按钮，裁决由 ApprovalReceiver 执行。 */
    private void addApprovalButtons(Notification.Builder b, String approvalId, int notifId) {
        String[][] specs = {{"批准", "approve"}, {"拒绝", "deny"}};
        for (String[] spec : specs) {
            Intent bi = new Intent(this, ApprovalReceiver.class);
            bi.putExtra("approval_id", approvalId);
            bi.putExtra("decision", spec[1]);
            bi.putExtra("notif_id", notifId);
            PendingIntent bp = PendingIntent.getBroadcast(this, notifId * 2
                            + ("approve".equals(spec[1]) ? 0 : 1), bi,
                    PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
            Notification.Action act = new Notification.Action.Builder(
                    android.graphics.drawable.Icon.createWithResource(this,
                            android.R.drawable.ic_input_get),
                    spec[0], bp).build();
            b.addAction(act);
        }
    }

    @Override
    public void onDestroy() {
        running = false;
        if (worker != null) {
            worker.interrupt();
        }
        super.onDestroy();
    }
}
