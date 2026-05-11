package com.subflow.tv

import android.annotation.SuppressLint
import android.app.Activity
import android.content.Intent
import android.os.Bundle
import android.view.View
import android.webkit.WebResourceRequest
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.ProgressBar

class TwitchAuthActivity : Activity() {

    companion object {
        const val EXTRA_TOKEN = "twitch_token"

        private const val CLIENT_ID   = "haofrfzyxtscxep60sfg9hek15ueh9"
        private const val REDIRECT_URI = "http://localhost"
        private const val SCOPE        = "user:read:follows"

        private val AUTH_URL =
            "https://id.twitch.tv/oauth2/authorize" +
            "?client_id=$CLIENT_ID" +
            "&redirect_uri=$REDIRECT_URI" +
            "&response_type=token" +
            "&scope=$SCOPE"
    }

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_twitch_auth)

        val webView  = findViewById<WebView>(ProgressBar@ R.id.authWebView)
        val progress = findViewById<ProgressBar>(R.id.authProgress)

        webView.settings.javaScriptEnabled = true

        webView.webViewClient = object : WebViewClient() {

            override fun onPageFinished(view: WebView, url: String) {
                progress.visibility = View.GONE
            }

            override fun shouldOverrideUrlLoading(
                view: WebView,
                request: WebResourceRequest
            ): Boolean {
                val url = request.url

                // Twitch redirects to http://localhost#access_token=TOKEN&...
                if (url.host == "localhost" || url.scheme == "http" && url.host == "localhost") {
                    val fragment = url.fragment ?: return true   // no fragment → ignore
                    val token = fragment
                        .split("&")
                        .firstOrNull { it.startsWith("access_token=") }
                        ?.removePrefix("access_token=")

                    if (token != null) {
                        val data = Intent().putExtra(EXTRA_TOKEN, token)
                        setResult(RESULT_OK, data)
                    } else {
                        setResult(RESULT_CANCELED)
                    }
                    finish()
                    return true
                }
                return false
            }
        }

        webView.loadUrl(AUTH_URL)
    }
}
