plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "com.zmux.terminal"
    compileSdk = 34

    defaultConfig {
        applicationId = "com.zmux.terminal"
        minSdk = 26
        targetSdk = 34
        versionCode = 1
        versionName = "0.1.0-poc"

        ndk {
            // Target: ARMv7 / Android Go class devices (Infinix Smart 9 HD) + modern arm64.
            abiFilters += listOf("armeabi-v7a", "arm64-v8a")
        }
    }

    buildTypes {
        debug {
            isMinifyEnabled = false
            applicationIdSuffix = ".debug"
        }
        release {
            isMinifyEnabled = false
            proguardFiles(getDefaultProguardFile("proguard-android-optimize.txt"), "proguard-rules.pro")
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    kotlinOptions {
        jvmTarget = "17"
    }

    buildFeatures {
        viewBinding = false
    }

    packaging {
        resources.excludes += "/META-INF/{AL2.0,LGPL2.1}"
    }
}

dependencies {
    // --- Termux terminal stack (Apache-2.0 libraries only, NOT the GPLv3 app) ---
    //
    // Coordinates per termux-app's "Termux Libraries" wiki page:
    //   * JitPack groupId is `com.termux.termux-app`
    //   * `com.termux` also resolves (termux.com carries the JitPack DNS TXT record)
    //   * terminal-emulator is a transitive dep of terminal-view, but we pin it
    //     explicitly so a version skew fails at resolution instead of at runtime.
    // Versions >= 0.116 are published; 0.118.0 is the last stable tag.
    implementation("com.termux.termux-app:terminal-view:0.118.0")
    implementation("com.termux.termux-app:terminal-emulator:0.118.0")

    // --- Transport to the existing Python ZMUX backend ---
    implementation("com.squareup.okhttp3:okhttp:4.12.0")

    // --- AndroidX (kept minimal for Android Go footprint) ---
    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.appcompat:appcompat:1.7.0")
    implementation("com.google.android.material:material:1.12.0")
    implementation("androidx.constraintlayout:constraintlayout:2.1.4")
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.8.1")
}
