package com.gongsheng.remote

import android.annotation.SuppressLint
import android.app.Activity
import android.content.SharedPreferences
import android.os.Bundle
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.TextView

/**
 * 共生遥控器 —— 手机端 WebView 壳。
 *
 * 设计（隐私纪律）：
 *  - **不在 APK 里硬编码服务地址**（本仓库公开，tailnet 域名不上库）。
 *  - 首次启动粘贴服务地址（含 ?token= 的完整引导链接），存本机 SharedPreferences。
 *  - 之后每次启动直接加载；长按标题可重填。
 *
 * 服务端四道校验（Host/Origin/X-Xiaoman-Request/Cookie）由 webui 页面自身的
 * JS 与引导流程完成——WebView 内与浏览器行为一致，无需额外处理。
 */
class MainActivity : Activity() {

    private lateinit var prefs: SharedPreferences
    private var web: WebView? = null

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        prefs = getSharedPreferences("gongsheng", MODE_PRIVATE)
        val saved = prefs.getString("server_url", "").orEmpty()
        if (saved.isBlank()) showSetup() else loadServer(saved)
    }

    @SuppressLint("SetJavaScriptEnabled")
    private fun loadServer(url: String) {
        val web = WebView(this)
        web.settings.javaScriptEnabled = true
        web.settings.domStorageEnabled = true
        web.webViewClient = object : WebViewClient() {
            // 引导链接 /?token=… 会 302 写 cookie——全部在 WebView 内消化
            override fun shouldOverrideUrlLoading(view: WebView, url: String): Boolean = false
        }
        setContentView(web)
        web.loadUrl(url)
        this.web = web
    }

    /** 首次运行：粘贴服务地址。 */
    private fun showSetup() {
        val box = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(56, 140, 56, 48)
        }
        box.addView(TextView(this).apply {
            text = "共生遥控器\n\n粘贴服务地址（含 ?token= 的完整链接）。\n需要手机已连接 Tailscale。"
            textSize = 16f
        })
        val input = EditText(this).apply {
            hint = "https://…/?token=…"
            singleLine = true
        }
        box.addView(input)
        box.addView(Button(this).apply {
            text = "连接"
            setOnClickListener {
                val url = input.text.toString().trim()
                if (url.startsWith("http")) {
                    prefs.edit().putString("server_url", url).apply()
                    loadServer(url)
                } else {
                    input.error = "必须是 http(s) 开头的完整地址"
                }
            }
        })
        setContentView(box)
    }

    override fun onBackPressed() {
        val w = web
        if (w != null && w.canGoBack()) w.goBack() else super.onBackPressed()
    }
}
