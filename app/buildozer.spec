[app]

title = ZMUX
package.name = zmux
package.domain = com.zaba
source.dir = .
source.include_exts = py,png,jpg,html,css,json,js
version = 1.0.2

# Icon
icon.filename = %(source.dir)s/assets/logo.png

# Presplash
presplash.filename = %(source.dir)s/assets/presplash.png
presplash_color = #0d1117

# WebView shell for terminal UI
# COEXISTENCE CONTRACT (2026): Zabacode owns 5000, Zmux owns 8000.
# Loopback 127.0.0.1 is shared, so both on 5000 caused "buka zabacode muncul zmux".
# Port 6000 was tried next but Chromium/Android WebView blocks it (X11) with
# net::ERR_UNSAFE_PORT. 8000 is Chromium-safe and clear of Zabacode's
# 5000-5100 range. Must stay in sync with P4A_HTTP_PORT in app/zmux/server.py.
p4a.bootstrap = webview
p4a.port = 8000

# Core requirements - minimal for terminal
requirements = python3,pyjnius,flask,waitress,packaging,certifi,werkzeug,jinja2,itsdangerous,click,blinker,MarkupSafe

orientation = portrait
fullscreen = 0

# Android specific
android.archs = armeabi-v7a, arm64-v8a

# --- PRoot / Alpine sandbox ------------------------------------------------
android.add_libs_armeabi_v7a = libs/armeabi-v7a/*.so
android.add_libs_arm64_v8a = libs/arm64-v8a/*.so
android.accept_sdk_license = True
android.api = 34
android.minapi = 26
android.ndk_api = 26
android.permissions = INTERNET, (name=android.permission.READ_EXTERNAL_STORAGE;maxSdkVersion=28), (name=android.permission.WRITE_EXTERNAL_STORAGE;maxSdkVersion=28)
android.uses_cleartext_traffic = True
android.allow_backup = False
android.orientation = portrait

# Coexistence: distinct taskAffinity + less aggressive launchMode
android.manifest.launch_mode = singleTop

# p4a hook to inject taskAffinity=com.zaba.zmux + documentLaunchMode
p4a.hook = tools/p4a_hook.py

# Adaptive Icon
android.adaptive_icon_background = #0d1117
android.adaptive_icon_foreground = %(source.dir)s/assets/logo.png

# Presplash color
android.presplash_color = #0d1117
android.presplash = %(source.dir)s/assets/presplash.png

[buildozer]
log_level = 2
warn_on_root = 1

# Reproducible ZMUX runtime contract
p4a.commit = 5c192d7b7308487c2d3e3fcae63deba3131e7cb2
android.ndk = 28c
