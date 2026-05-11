package com.subflow.tv

import android.annotation.SuppressLint
import android.app.Activity
import android.content.Context
import android.content.Intent
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.view.View
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.TextView
import android.widget.Toast
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL
import java.util.concurrent.Executors

class WatchActivity : AppCompatActivity() {

    // ── configuration ────────────────────────────────────────────────────────
    companion object {
        // Replace with your PC's LAN IP address (device localhost ≠ PC)
        private const val API_BASE     = "http://192.168.1.100:5000"
        private const val PREFS_NAME   = "subflow_prefs"
        private const val KEY_DELAY    = "subtitle_delay"
        private const val KEY_TOKEN    = "twitch_token"
        private const val KEY_USER_ID  = "twitch_user_id"
        private const val KEY_NAME     = "twitch_name"
        private const val KEY_QUALITY  = "quality"
        private const val CLIENT_ID    = "haofrfzyxtscxep60sfg9hek15ueh9"
        private const val POLL_MS      = 1500L
        private const val MIN_DELAY    = 0
        private const val MAX_DELAY    = 15
    }

    // ── views ────────────────────────────────────────────────────────────────
    private lateinit var streamWebView : WebView
    private lateinit var btn480p       : TextView
    private lateinit var btn720p       : TextView
    private lateinit var btnAuto       : TextView
    private lateinit var btnLogin      : TextView
    private lateinit var tvSubtitle    : TextView
    private lateinit var tvOriginal    : TextView
    private lateinit var tvDelay       : TextView
    private lateinit var btnDelayMinus : TextView
    private lateinit var btnDelayPlus  : TextView

    // ── state ────────────────────────────────────────────────────────────────
    private var subtitleDelay  = 3       // seconds
    private var selectedQuality = "auto"
    private var twitchToken    : String? = null
    private var twitchUserId   : String? = null
    private var twitchName     : String? = null

    private val handler    = Handler(Looper.getMainLooper())
    private val executor   = Executors.newSingleThreadExecutor()
    private val scheduled  = mutableSetOf<String>()  // texts currently in delay
    private var lastText   = ""
    private var polling    = false

    // ── OAuth launcher ───────────────────────────────────────────────────────
    private val authLauncher = registerForActivityResult(
        ActivityResultContracts.StartActivityForResult()
    ) { result ->
        if (result.resultCode == Activity.RESULT_OK) {
            val token = result.data?.getStringExtra(TwitchAuthActivity.EXTRA_TOKEN)
            if (token != null) onTokenReceived(token)
        }
    }

    // ── lifecycle ────────────────────────────────────────────────────────────
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_watch)
        bindViews()
        loadPrefs()
        setupWebView()
        setupQualityButtons()
        setupDelayButtons()
        setupLoginButton()
        updateDelayLabel()
        startPolling()
    }

    override fun onDestroy() {
        super.onDestroy()
        polling = false
        handler.removeCallbacksAndMessages(null)
        executor.shutdownNow()
        streamWebView.destroy()
    }

    // ── view binding ─────────────────────────────────────────────────────────
    private fun bindViews() {
        streamWebView  = findViewById(R.id.streamWebView)
        btn480p        = findViewById(R.id.btn480p)
        btn720p        = findViewById(R.id.btn720p)
        btnAuto        = findViewById(R.id.btnAuto)
        btnLogin       = findViewById(R.id.btnLogin)
        tvSubtitle     = findViewById(R.id.tvSubtitle)
        tvOriginal     = findViewById(R.id.tvOriginal)
        tvDelay        = findViewById(R.id.tvDelay)
        btnDelayMinus  = findViewById(R.id.btnDelayMinus)
        btnDelayPlus   = findViewById(R.id.btnDelayPlus)
    }

    // ── preferences ──────────────────────────────────────────────────────────
    private fun loadPrefs() {
        val prefs = getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
        subtitleDelay   = prefs.getInt(KEY_DELAY, 3).coerceIn(MIN_DELAY, MAX_DELAY)
        selectedQuality = prefs.getString(KEY_QUALITY, "auto") ?: "auto"
        twitchToken     = prefs.getString(KEY_TOKEN, null)
        twitchUserId    = prefs.getString(KEY_USER_ID, null)
        twitchName      = prefs.getString(KEY_NAME, null)

        if (twitchToken != null) {
            btnLogin.text = "✓  ${twitchName ?: "Twitch"}"
        }
    }

    private fun savePrefs() {
        getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE).edit().apply {
            putInt(KEY_DELAY, subtitleDelay)
            putString(KEY_QUALITY, selectedQuality)
            putString(KEY_TOKEN, twitchToken)
            putString(KEY_USER_ID, twitchUserId)
            putString(KEY_NAME, twitchName)
            apply()
        }
    }

    // ── WebView setup ─────────────────────────────────────────────────────────
    @SuppressLint("SetJavaScriptEnabled")
    private fun setupWebView() {
        streamWebView.settings.apply {
            javaScriptEnabled          = true
            domStorageEnabled          = true
            mediaPlaybackRequiresUserGesture = false
            cacheMode                  = WebSettings.LOAD_NO_CACHE
            useWideViewPort            = true
            loadWithOverviewMode       = true
        }
        streamWebView.webViewClient = object : WebViewClient() {
            override fun onPageFinished(view: WebView, url: String) {
                // Inject auth cookie so Twitch player is authenticated
                twitchToken?.let { injectTwitchCookie(it) }
            }
        }

        // If a channel was passed via Intent, load it immediately
        intent.getStringExtra("channel")?.let { loadChannel(it) }
    }

    private fun injectTwitchCookie(token: String) {
        // Set document.cookie via JS for .twitch.tv — covers both player & chat
        streamWebView.evaluateJavascript(
            "document.cookie = 'auth-token=$token; domain=.twitch.tv; path=/';",
            null
        )
    }

    private fun loadChannel(channel: String) {
        val quality = when (selectedQuality) {
            "480p" -> "low"
            "720p" -> "high"
            else   -> "auto"
        }
        val url = "https://player.twitch.tv/?channel=$channel" +
                  "&parent=player.twitch.tv&autoplay=true&muted=false&quality=$quality"
        streamWebView.loadUrl(url)
    }

    // ── quality buttons ───────────────────────────────────────────────────────
    private fun setupQualityButtons() {
        val buttons = mapOf(
            btn480p to "480p",
            btn720p to "720p",
            btnAuto to "auto"
        )
        buttons.forEach { (btn, quality) ->
            btn.setOnClickListener { selectQuality(quality, buttons) }
        }
        // Reflect loaded preference
        val active = buttons.entries.firstOrNull { it.value == selectedQuality }?.key
        buttons.keys.forEach { it.isSelected = (it == active) }
    }

    private fun selectQuality(quality: String, buttons: Map<TextView, String>) {
        selectedQuality = quality
        buttons.forEach { (btn, q) -> btn.isSelected = (q == quality) }
        savePrefs()
        // Reload current URL with new quality if a stream is playing
        val url = streamWebView.url ?: return
        if (url.contains("player.twitch.tv") || url.contains("youtube.com") || url.contains("kick.com")) {
            streamWebView.reload()
        }
    }

    // ── delay buttons ─────────────────────────────────────────────────────────
    private fun setupDelayButtons() {
        btnDelayMinus.setOnClickListener {
            if (subtitleDelay > MIN_DELAY) {
                subtitleDelay--
                updateDelayLabel()
                savePrefs()
            }
        }
        btnDelayPlus.setOnClickListener {
            if (subtitleDelay < MAX_DELAY) {
                subtitleDelay++
                updateDelayLabel()
                savePrefs()
            }
        }
    }

    private fun updateDelayLabel() {
        tvDelay.text = "Delay: ${subtitleDelay}s"
    }

    // ── login button ──────────────────────────────────────────────────────────
    private fun setupLoginButton() {
        btnLogin.setOnClickListener {
            authLauncher.launch(Intent(this, TwitchAuthActivity::class.java))
        }
    }

    private fun onTokenReceived(token: String) {
        twitchToken = token
        executor.execute {
            try {
                val user = fetchJson(
                    "https://api.twitch.tv/helix/users",
                    token
                )
                val userData = user.getJSONArray("data").getJSONObject(0)
                twitchUserId = userData.getString("id")
                twitchName   = userData.getString("display_name")

                handler.post {
                    btnLogin.text = "✓  $twitchName"
                    savePrefs()
                    Toast.makeText(this, "Logged in as $twitchName", Toast.LENGTH_SHORT).show()
                }
            } catch (e: Exception) {
                handler.post {
                    Toast.makeText(this, "Twitch login failed: ${e.message}", Toast.LENGTH_LONG).show()
                }
            }
        }
    }

    // ── subtitle polling ──────────────────────────────────────────────────────
    private fun startPolling() {
        polling = true
        schedulePoll()
    }

    private fun schedulePoll() {
        if (!polling) return
        handler.postDelayed({ doPoll() }, POLL_MS)
    }

    private fun doPoll() {
        executor.execute {
            try {
                val json  = fetchJson("$API_BASE/latest", null)
                val text  = json.optString("text", "")
                val orig  = json.optString("original", "")
                handler.post { onNewText(text, orig) }
            } catch (_: Exception) { }
            schedulePoll()
        }
    }

    private fun onNewText(text: String, original: String) {
        if (text.isEmpty() || text == lastText || scheduled.contains(text)) return
        lastText = text
        scheduled.add(text)

        if (subtitleDelay <= 0) {
            showSubtitle(text, original)
            scheduled.remove(text)
        } else {
            handler.postDelayed({
                showSubtitle(text, original)
                scheduled.remove(text)
            }, subtitleDelay * 1000L)
        }
    }

    private fun showSubtitle(text: String, original: String) {
        tvSubtitle.text = text
        tvOriginal.text = if (original.isNotEmpty()) original else ""
    }

    // ── HTTP helper ───────────────────────────────────────────────────────────
    private fun fetchJson(urlString: String, token: String?): JSONObject {
        val conn = (URL(urlString).openConnection() as HttpURLConnection).apply {
            connectTimeout = 5_000
            readTimeout    = 5_000
            if (token != null) {
                setRequestProperty("Authorization", "Bearer $token")
                setRequestProperty("Client-Id", CLIENT_ID)
            }
        }
        return try {
            val body = conn.inputStream.bufferedReader().readText()
            JSONObject(body)
        } finally {
            conn.disconnect()
        }
    }
}
